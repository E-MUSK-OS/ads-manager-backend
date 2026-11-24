from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
import math
from typing import Any, Optional, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.core.marketplace_rules import marketplace_or_in, today_for
from app.models.ads_account import AdsAccount
from app.models.campaign import Campaign
from app.models.ad_group import AdGroup
from app.models.keyword import Keyword
from app.models.metric_daily import MetricDaily
from app.models.placement_metric_daily import PlacementMetricDaily
from app.models.search_term import SearchTerm
from app.models.recommendation import Recommendation
from app.models.recommendation_rule import RecommendationRule, RecommendationRuleCampaign
from app.models.recommendation_run import RecommendationRun
from app.models.campaign_bid_threshold import CampaignBidThreshold
from app.models.campaign_status_daily import CampaignStatusDaily
from app.models.campaign_history import CampaignHistory
from app.models.negative_keyword import NegativeKeyword
from app.recommendations.dates import resolve_date_filter, CONVERSION_METRICS, is_in_freeze_period
from app.recommendations.registry import LIMITS, ACTIONS
from app.services.metrics_service import derive


@dataclass
class Candidate:
    rule: Any
    entity_type: str  # campaign | target | search_term | placement
    entity_id: Optional[int]
    entity_label: str
    campaign_id: int
    ad_group_id: Optional[int]
    metrics: dict[str, Any]
    matched: list[dict[str, Any]]
    suggested_actions: list[dict[str, Any]]
    projection: Optional[dict[str, Any]]
    dedupe_key: str
    current: dict[str, Any] = field(default_factory=dict)
    date_window: dict[str, Any] = field(default_factory=dict)
    confidence: Optional[dict[str, Any]] = None
    tracked_since: Optional[str] = None
    safety_flag: Optional[str] = None
    safety_warning: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": getattr(self.rule, "id", None),
            "rule_name": getattr(self.rule, "name", ""),
            "affected_parameter": getattr(self.rule, "affected_parameter", ""),
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "entity_label": self.entity_label,
            "campaign_id": self.campaign_id,
            "ad_group_id": self.ad_group_id,
            "metrics": self.metrics,
            "matched": self.matched,
            "suggested_actions": self.suggested_actions,
            "projection": self.projection,
            "dedupe_key": self.dedupe_key,
            "current": self.current,
            "date_window": self.date_window,
            "confidence": self.confidence,
            "tracked_since": self.tracked_since,
            "safety_flag": self.safety_flag,
            "safety_warning": self.safety_warning,
            "evidence": {
                "metrics": self.metrics,
                "matched": self.matched,
                "current": self.current,
                "projection": self.projection,
                "date_window": self.date_window,
                **({"confidence": self.confidence} if self.confidence else {}),
                **({"tracked_since": self.tracked_since} if self.tracked_since else {}),
                **({"safety_flag": self.safety_flag} if self.safety_flag else {}),
                **({"safety_warning": self.safety_warning} if self.safety_warning else {}),
            },
        }


def compute_confidence(
    metrics: dict[str, Any],
    conditions: list[Any],
    ad_group_ref: Optional[tuple[int, int]] = None,
    campaign_ref: Optional[tuple[int, int]] = None,
) -> Optional[dict[str, Any]]:
    # Confidence label for rules that contain conversion-based drops (orders <= 0, etc.)
    has_orders_zero = False
    for c in conditions:
        m = c.get("metric") if isinstance(c, dict) else getattr(c, "metric", "")
        op = c.get("op") if isinstance(c, dict) else getattr(c, "op", "")
        val = c.get("value") if isinstance(c, dict) else getattr(c, "value", None)
        if m in ("orders", "purchases") and op in ("LTE", "EQ", "<=", "==") and float(val or 0) <= 0:
            has_orders_zero = True
            break
    if not has_orders_zero:
        return None

    target_clicks = int(metrics.get("clicks") or 0)

    # 1. Same ad group reference (sums over window)
    if ad_group_ref is not None and ad_group_ref[0] >= 30:
        ref_level = "ad_group"
        ref_clicks, ref_orders = ad_group_ref
    elif campaign_ref is not None and campaign_ref[0] >= 30:
        ref_level = "campaign"
        ref_clicks, ref_orders = campaign_ref
    else:
        # Reference has < 30 clicks -> "Not enough data"
        chosen = ad_group_ref if (ad_group_ref and ad_group_ref[0] > 0) else campaign_ref
        ref_level = "ad_group" if ad_group_ref is not None else "campaign"
        ref_clicks = chosen[0] if chosen else 0
        ref_orders = chosen[1] if chosen else 0
        ref_cvr = (ref_orders / ref_clicks) if ref_clicks > 0 else 0.0
        return {
            "clicks": target_clicks,
            "ref_cvr": round(float(ref_cvr), 4),
            "reference_cvr": round(float(ref_cvr), 4),
            "ref_level": ref_level,
            "ref_clicks": ref_clicks,
            "p": None,
            "p_value": None,
            "label": "Not enough data",
            "level": "LOW_DATA",
        }

    ref_cvr = (ref_orders / ref_clicks) if ref_clicks > 0 else 0.0
    ref_cvr = min(1.0, max(0.0, ref_cvr))
    p = (1.0 - ref_cvr) ** target_clicks

    if p < 0.05:
        label = "Strong evidence"
        level = "HIGH"
    else:
        label = "Weak evidence"
        level = "MEDIUM"

    return {
        "clicks": target_clicks,
        "ref_cvr": round(float(ref_cvr), 4),
        "reference_cvr": round(float(ref_cvr), 4),
        "ref_level": ref_level,
        "ref_clicks": ref_clicks,
        "p": round(float(p), 4),
        "p_value": round(float(p), 4),
        "label": label,
        "level": level,
    }


def pct_change(value: Any, pct: Any, up: bool) -> Decimal:
    v = Decimal(str(value))
    p = Decimal(str(pct)) / Decimal(100)
    r = v * (Decimal(1) + p if up else Decimal(1) - p)
    return r.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_decimal_bid_change(
    current_bid: Any,
    pct: Any = None,
    up: bool = True,
    min_bid: Decimal = Decimal("1.00"),
    max_bid: Decimal = Decimal("5000.00"),
    mode: str = "percent",
    value: Any = None,
) -> tuple[Decimal, Decimal]:
    curr = Decimal(str(current_bid)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if mode == "amount":
        val_to_use = value if value is not None else pct
        delta = Decimal(str(val_to_use)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        new_bid = (curr + delta) if up else (curr - delta)
    else:
        val_to_use = value if value is not None else (pct if pct is not None else 10)
        p = Decimal(str(val_to_use)) / Decimal(100)
        r = curr * (Decimal(1) + p if up else Decimal(1) - p)
        new_bid = r.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    clamped = min(max(new_bid, min_bid), max_bid)
    return curr, clamped


def compute_decimal_budget_change(
    current_budget: Any,
    pct: Any = None,
    up: bool = True,
    min_budget: Decimal = Decimal("100.00"),
    max_budget: Decimal = Decimal("20000000.00"),
    mode: str = "percent",
    value: Any = None,
) -> tuple[Decimal, Decimal]:
    curr = Decimal(str(current_budget)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if mode == "amount":
        val_to_use = value if value is not None else pct
        delta = Decimal(str(val_to_use)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        new_budget = (curr + delta) if up else (curr - delta)
    else:
        val_to_use = value if value is not None else (pct if pct is not None else 10)
        p = Decimal(str(val_to_use)) / Decimal(100)
        r = curr * (Decimal(1) + p if up else Decimal(1) - p)
        new_budget = r.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    clamped = min(max(new_budget, min_budget), max_budget)
    return curr, clamped


def compute_pct_change(curr: Optional[float], prev: Optional[float]) -> Optional[float]:
    """
    Computes percentage change between current and previous period.
    Per Hard Rule 8: zero denominator = null.
    """
    if prev is None or prev == 0:
        if curr == 0 or curr is None:
            return 0.0
        return None  # Zero denominator -> null
    if curr is None:
        curr = 0.0
    return round(((curr - prev) / prev) * 100.0, 2)


def format_date_window_label(start: date, end: date) -> str:
    if start.year == end.year:
        if start.month == end.month:
            return f"{start.day} - {end.day} {end.strftime('%b %Y')}"
        return f"{start.strftime('%d %b')} - {end.strftime('%d %b %Y')}"
    return f"{start.strftime('%d %b %Y')} - {end.strftime('%d %b %Y')}"


def format_cond_text(c: dict[str, Any], in_past: bool = False) -> str:
    m = c.get("metric", "")
    op = c.get("op", "")
    thresh = c.get("threshold", 0)
    thresh_val = float(thresh) if thresh is not None else 0.0
    thresh_str = (
        f"₹{int(thresh_val) if thresh_val == int(thresh_val) else thresh_val:g}"
        if m in ("spend", "cpc")
        else (
            f"{int(thresh_val) if thresh_val == int(thresh_val) else thresh_val:g}%"
            if m in ("acos", "ctr", "cvr") or m.endswith("_change_pct") or m.endswith("_share")
            else (
                f"{int(thresh_val):g} days"
                if m in ("out_of_budget_days", "entity_age_days")
                else f"{int(thresh_val) if thresh_val == int(thresh_val) else thresh_val:g}"
            )
        )
    )
    if in_past:
        if m == "acos_vs_target":
            sign = f"+{thresh_val:g}" if thresh_val > 0 else (f"{thresh_val:g}" if thresh_val < 0 else "")
            off_str = f" {sign}%" if sign else ""
            return f"had ACOS {op.lower()} Target ACOS{off_str}"
        elif m == "out_of_budget_days":
            return f"was out of budget for at least {thresh_val:g} days" if op in ("GTE", "GT") else f"was out of budget for {op.lower()} {thresh_val:g} days"
        elif m == "spend":
            return f"spent at least {thresh_str}" if op in ("GTE", "GT") else f"spent less than {thresh_str}"
        elif m == "orders":
            if op in ("LTE", "EQ") and thresh_val <= 0:
                return "had 0 orders"
            return f"had {op.lower()} {thresh_str} orders"
        elif m == "clicks":
            return f"had at least {thresh_str} clicks"
        return f"had {m} {op.lower()} {thresh_str}"
    else:
        if m == "acos_vs_target":
            op_sym = {">=": ">=", "<=": "<=", "==": "=", "!=": "!=", "GTE": ">=", "LTE": "<=", "GT": ">", "LT": "<", "EQ": "="}.get(op, op)
            sign = f"+{thresh_val:g}" if thresh_val > 0 else (f"{thresh_val:g}" if thresh_val < 0 else "")
            off_str = f" {sign}%" if sign else ""
            return f"ACOS {op_sym} Target ACOS{off_str}"
        if m == "out_of_budget_days":
            op_sym = {">=": ">=", "<=": "<=", "==": "=", "!=": "!=", "GTE": ">=", "LTE": "<=", "GT": ">", "LT": "<", "EQ": "="}.get(op, op)
            return f"out of budget {op_sym} {thresh_val:g} days"
        if m == "orders" and op in ("LTE", "EQ") and thresh_val <= 0:
            return "orders = 0"
        op_sym = {
            ">=": ">=",
            "<=": "<=",
            "==": "=",
            "!=": "!=",
            "GTE": ">=",
            "LTE": "<=",
            "GT": ">",
            "LT": "<",
            "EQ": "=",
        }.get(op, op)
        return f"{m} {op_sym} {thresh_str}"


def build_funnel_summary(
    funnel: dict[str, Any],
    rule: Any,
    account: AdsAccount,
) -> tuple[str, str, str]:
    affected_param = getattr(rule, "affected_parameter", "")
    param_label = {
        "TARGETING": "targets",
        "CAMPAIGN": "campaigns",
        "SEARCH_TERM": "search terms",
        "PLACEMENT": "placements",
    }.get(affected_param, "items")

    entities_in_scope = funnel.get("entities_in_scope", 0)
    enabled_camps = funnel.get("enabled_campaigns_count", 0)
    final_candidates = funnel.get("final_candidates", 0)
    min_clicks = getattr(rule, "min_clicks", 0) or 0
    conditions = funnel.get("conditions", [])
    date_label = funnel.get("date_window", {}).get("label", "")
    eval_iso = funnel.get("evaluated_at")

    if eval_iso:
        try:
            eval_dt = datetime.fromisoformat(eval_iso)
            eval_str = eval_dt.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%d %b, %H:%M IST")
        except Exception:
            eval_str = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b, %H:%M IST")
    else:
        eval_str = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b, %H:%M IST")

    # 1. Summary text (for Add/Edit Modal Preview)
    if funnel.get("skipped_reason"):
        summary_text = f"0 items match right now. {funnel['skipped_reason']}."
        config_status_text = f"Evaluated {eval_str}: 0 recommendations — {funnel['skipped_reason']} ({date_label})"
        tip = funnel.get("tip") or "Verify tracking and synchronize account data."
        return summary_text, config_status_text, tip

    if entities_in_scope == 0:
        summary_text = "0 items match right now. No entities found in the selected scope."
    elif final_candidates > 0:
        summary_text = (
            f"{final_candidates} item{'s' if final_candidates != 1 else ''} match right now. "
            f"{entities_in_scope} {param_label} in {enabled_camps} enabled campaign{'s' if enabled_camps != 1 else ''}; "
        )
        cond_parts = [f"{c.get('passed', 0)} {format_cond_text(c, in_past=True)}" for c in conditions]
        summary_text += "; ".join(cond_parts)
        if min_clicks > 0:
            summary_text += f" and {min_clicks}+ clicks."
        else:
            summary_text += "."
    else:
        summary_text = (
            f"0 items match right now. "
            f"{entities_in_scope} {param_label} in {enabled_camps} enabled campaign{'s' if enabled_camps != 1 else ''}; "
        )
        if funnel.get("no_traffic", 0) == entities_in_scope and entities_in_scope > 0:
            summary_text += "none had traffic in this date range."
        elif min_clicks > 0 and funnel.get("below_min_clicks", 0) == (
            entities_in_scope
            - funnel.get("no_traffic", 0)
            - funnel.get("excluded_campaign_not_enabled", 0)
            - funnel.get("excluded_target_not_enabled", 0)
        ) and funnel.get("below_min_clicks", 0) > 0:
            summary_text += f"none reached the minimum threshold of {min_clicks} clicks."
        else:
            cond_parts = []
            for idx, c in enumerate(conditions):
                p = c.get("passed", 0)
                if p > 0:
                    cond_parts.append(f"{p} {format_cond_text(c, in_past=True)}")
                else:
                    clicks_part = f" and {min_clicks}+ clicks" if min_clicks > 0 else ""
                    cond_parts.append(f"none of those had {format_cond_text(c, in_past=False)}{clicks_part}")
                    break
            summary_text += "; ".join(cond_parts)
            if not summary_text.endswith("."):
                summary_text += "."

    # 2. Config status text (for Configuration tab row)
    if final_candidates > 0:
        config_status_text = f"Evaluated {eval_str}: {final_candidates} recommendation{'s' if final_candidates != 1 else ''} match ({date_label})"
    else:
        if entities_in_scope == 0:
            config_status_text = f"Evaluated {eval_str}: 0 recommendations — no entities in scope ({date_label})"
        elif funnel.get("no_traffic", 0) == entities_in_scope and entities_in_scope > 0:
            config_status_text = f"Evaluated {eval_str}: 0 recommendations — no traffic in window ({date_label})"
        elif conditions:
            first_c = conditions[0]
            first_m = first_c.get("metric", "condition")
            first_p = first_c.get("passed", 0)
            if len(conditions) > 1 and first_p > 0:
                second_c = conditions[1]
                second_m = second_c.get("metric", "condition")
                config_status_text = f"Evaluated {eval_str}: 0 recommendations — {first_p} of {entities_in_scope} {param_label} passed {first_m}, none passed {second_m} ({date_label})"
            else:
                config_status_text = f"Evaluated {eval_str}: 0 recommendations — none of {entities_in_scope} {param_label} passed {first_m} ({date_label})"
        else:
            config_status_text = f"Evaluated {eval_str}: 0 recommendations ({date_label})"

    # 3. Tip (First condition that removes the most items)
    bottlenecks: list[tuple[str, int, Optional[dict[str, Any]]]] = []
    if funnel.get("no_traffic", 0) > 0:
        bottlenecks.append(("no_traffic", funnel["no_traffic"], None))
    if funnel.get("below_min_clicks", 0) > 0:
        bottlenecks.append(("min_clicks", funnel["below_min_clicks"], None))
    for c in conditions:
        if c.get("failed", 0) > 0:
            bottlenecks.append(("condition", c["failed"], c))
    if funnel.get("skipped_already_at_limit", 0) > 0:
        bottlenecks.append(("at_limit", funnel["skipped_already_at_limit"], None))
    if funnel.get("skipped_bid_unknown", 0) > 0:
        bottlenecks.append(("bid_unknown", funnel["skipped_bid_unknown"], None))
    if funnel.get("skipped_no_target_acos", 0) > 0:
        bottlenecks.append(("no_target_acos", funnel["skipped_no_target_acos"], None))

    if not bottlenecks:
        tip = "All items match these conditions."
    else:
        top_kind, top_count, top_cond = max(bottlenecks, key=lambda x: x[1])
        if top_kind == "no_traffic":
            tip = "Widen the date range or verify tracking data for these campaigns."
        elif top_kind == "min_clicks":
            tip = f"Lower the minimum clicks condition (currently {min_clicks}) or widen the date range."
        elif top_kind == "condition" and top_cond:
            m = top_cond.get("metric", "")
            if m == "spend":
                tip = "Lower the spend condition or widen the date range."
            elif m in ("clicks", "min_clicks"):
                tip = "Lower the click condition or widen the date range."
            elif m in ("orders", "sales", "cvr"):
                tip = "Lower the click condition or widen the date range."
            elif m == "acos":
                tip = "Increase the ACOS threshold or widen the date range."
            elif m == "roas":
                tip = "Lower the ROAS threshold or widen the date range."
            else:
                tip = f"Adjust the {m} condition or widen the date range."
        elif top_kind == "at_limit":
            tip = "Targets already have bids at the marketplace minimum or maximum limit."
        elif top_kind == "bid_unknown":
            tip = "Set explicit bids or ad group default bids for these targets."
        elif top_kind == "no_target_acos":
            tip = "Set Target ACOS on campaigns or campaign bid thresholds to evaluate ACOS vs Target."
        else:
            tip = "Adjust conditions or widen the date range."

    return summary_text, config_status_text, tip


class RecommendationEngine(Protocol):
    async def evaluate(
        self,
        db: AsyncSession,
        account: AdsAccount,
        rules: list[Any],
        *,
        today: date,
        dry_run: bool = False,
    ) -> tuple[list[Candidate], dict[int | str, dict[str, Any]]]:
        ...


class RuleBasedEngine:
    def __init__(self) -> None:
        self.last_evaluated_count: int = 0
        self.last_funnels: dict[int | str, dict[str, Any]] = {}

    async def evaluate(
        self,
        db: AsyncSession,
        account: AdsAccount,
        rules: list[Any],
        *,
        today: date,
        dry_run: bool = False,
    ) -> tuple[list[Candidate], dict[int | str, dict[str, Any]]]:
        all_candidates: list[Candidate] = []
        total_evaluated_count = 0
        m_rules = marketplace_or_in(account.marketplace)
        funnels: dict[int | str, dict[str, Any]] = {}

        # Pre-fetch all campaigns for this account
        camp_res = await db.execute(
            select(Campaign).where(Campaign.ads_account_id == account.id)
        )
        all_campaigns = camp_res.scalars().all()
        all_enabled_campaigns = [c for c in all_campaigns if c.state == "ENABLED"]

        for rule in rules:
            if not dry_run and not getattr(rule, "is_active", True):
                continue

            date_filter = getattr(rule, "date_filter", "LAST_30_DAYS")
            conditions = getattr(rule, "conditions", []) or []
            has_conv = any(
                (c.get("metric") if isinstance(c, dict) else getattr(c, "metric", "")) in CONVERSION_METRICS
                for c in conditions
            )
            start, end = resolve_date_filter(date_filter, today, has_conversion_metrics=has_conv)
            days = max((end - start).days + 1, 1)
            date_window_label = format_date_window_label(start, end)
            date_window_dict = {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "days": days,
                "label": f"Evaluated {start.strftime('%d %b')} - {end.strftime('%d %b')}",
                "has_conversion_metrics": has_conv,
            }

            # Resolve campaigns by scope
            scope_mode = getattr(rule, "scope_mode", "ALL")
            if scope_mode in ("INCLUDE", "EXCLUDE"):
                if hasattr(rule, "campaign_ids") and rule.campaign_ids:
                    scope_campaign_ids = set(rule.campaign_ids)
                elif getattr(rule, "id", None) and isinstance(rule.id, int):
                    rc_res = await db.execute(
                        select(RecommendationRuleCampaign.campaign_id).where(
                            RecommendationRuleCampaign.rule_id == rule.id
                        )
                    )
                    scope_campaign_ids = set(rc_res.scalars().all())
                else:
                    scope_campaign_ids = set()

                if scope_mode == "INCLUDE":
                    campaigns_in_scope = [c for c in all_campaigns if c.id in scope_campaign_ids]
                else:  # EXCLUDE
                    campaigns_in_scope = [c for c in all_campaigns if c.id not in scope_campaign_ids]
            else:  # ALL
                campaigns_in_scope = list(all_campaigns)

            actions = getattr(rule, "actions", []) or []
            is_enable_rule = any(
                (a.get("type") if isinstance(a, dict) else getattr(a, "type", "")) == "ENABLE_CAMPAIGN"
                for a in actions
            )

            enabled_campaigns_in_scope = [c for c in campaigns_in_scope if c.state == "ENABLED"]
            if is_enable_rule:
                # ENABLE_CAMPAIGN evaluates PAUSED (not ARCHIVED) only per Q2
                campaigns_to_evaluate = [c for c in campaigns_in_scope if c.state == "PAUSED"]
                excluded_camp_not_enabled = sum(1 for c in campaigns_in_scope if c.state != "PAUSED")
            else:
                campaigns_to_evaluate = enabled_campaigns_in_scope
                excluded_camp_not_enabled = len(campaigns_in_scope) - len(campaigns_to_evaluate)

            active_campaign_ids = [c.id for c in campaigns_to_evaluate]
            camp_map = {c.id: c for c in campaigns_to_evaluate}
            scope_camp_ids = [c.id for c in campaigns_in_scope]

            # Preload Bid Thresholds & Target ACOS from Bid Threshold or Campaign (R3.2, Q4, Q6)
            bt_map: dict[int, CampaignBidThreshold] = {}
            if scope_camp_ids:
                bt_res = await db.execute(
                    select(CampaignBidThreshold).where(
                        CampaignBidThreshold.campaign_id.in_(scope_camp_ids)
                    )
                )
                bt_map = {bt.campaign_id: bt for bt in bt_res.scalars().all()}
            target_acos_by_camp = {
                c.id: (bt_map[c.id].target_acos if (c.id in bt_map and bt_map[c.id].target_acos is not None) else c.target_acos)
                for c in campaigns_in_scope
            }
            has_acos_vs_target = any(
                (c.get("metric") if isinstance(c, dict) else getattr(c, "metric", "")) == "acos_vs_target"
                for c in conditions
            )

            # Entity Change Guard (Q5: checks CampaignHistory within N days, off/empty by default)
            entity_guard_days = getattr(settings, "ENTITY_CHANGE_GUARD_DAYS", None)
            recently_changed_entities: set[tuple[str, int]] = set()
            if entity_guard_days is not None and entity_guard_days > 0 and scope_camp_ids:
                guard_cutoff = datetime.now(timezone.utc) - timedelta(days=entity_guard_days)
                recent_hist_res = await db.execute(
                    select(CampaignHistory.campaign_id, CampaignHistory.entity_type, CampaignHistory.entity_id)
                    .where(
                        CampaignHistory.campaign_id.in_(scope_camp_ids),
                        CampaignHistory.created_at >= guard_cutoff,
                    )
                )
                for r in recent_hist_res.all():
                    if r[0]:
                        recently_changed_entities.add(("campaign", r[0]))
                    if r[1] and r[2]:
                        recently_changed_entities.add((r[1], r[2]))

            # Data Completeness Guard (Q8: missing dates in window -> skip conversion rules)
            # Only applies to CAMPAIGN, TARGETING, PLACEMENT (SEARCH_TERM uses SearchTerm table)
            affected_param = getattr(rule, "affected_parameter", "")
            missing_dates: list[date] = []
            if affected_param != "SEARCH_TERM" and scope_camp_ids and (end >= start):
                dates_res = await db.execute(
                    select(func.distinct(MetricDaily.date)).where(
                        MetricDaily.entity_type == "campaign",
                        MetricDaily.entity_id.in_(scope_camp_ids),
                        MetricDaily.date >= start,
                        MetricDaily.date <= end,
                    )
                )
                present_dates = {
                    date.fromisoformat(d) if isinstance(d, str) else d
                    for d in dates_res.scalars().all()
                    if d is not None
                }
                all_dates = {start + timedelta(days=i) for i in range((end - start).days + 1)}
                missing_dates = sorted(list(all_dates - present_dates))

            has_conv = any(
                (c.get("metric") if isinstance(c, dict) else getattr(c, "metric", "")) in CONVERSION_METRICS
                for c in conditions
            )
            has_trend = any(
                (c.get("metric") if isinstance(c, dict) else getattr(c, "metric", "")).endswith("_change_pct")
                for c in conditions
            )
            window_days = (end - start).days + 1
            prev_end = start - timedelta(days=1)
            prev_start = prev_end - timedelta(days=window_days - 1)
            lookback_limit = today - timedelta(days=getattr(settings, "AMAZON_REPORTING_LOOKBACK_DAYS", 60))

            affected_param = getattr(rule, "affected_parameter", "")
            rule_id_str = str(getattr(rule, "id", "draft") or "draft")
            min_clicks = getattr(rule, "min_clicks", 0) or 0

            # Early skip for incomplete conversion data (Q8)
            if missing_dates and has_conv:
                funnel_dict = {
                    "rule_id": rule_id_str,
                    "evaluated_at": datetime.now(timezone.utc).isoformat(),
                    "date_window": date_window_dict,
                    "entities_in_scope": len(campaigns_in_scope),
                    "enabled_campaigns_count": len(enabled_campaigns_in_scope),
                    "conditions": [],
                    "final_candidates": 0,
                    "missing_data_days": [d.isoformat() for d in missing_dates],
                    "skipped_reason": f"Conversion rule skipped: missing metric data for {len(missing_dates)} day(s)",
                    "tip": f"Missing metric data for dates: {', '.join(d.isoformat() for d in missing_dates[:3])}. Run a sync to populate these dates before evaluating conversion rules.",
                }
                summary_text, config_status_text, tip = build_funnel_summary(funnel_dict, rule, account)
                funnel_dict["summary_text"] = summary_text
                funnel_dict["config_status_text"] = config_status_text
                funnel_dict["tip"] = tip
                funnels[rule_id_str] = funnel_dict
                if hasattr(rule, "id") and rule.id is not None:
                    funnels[rule.id] = funnel_dict
                if not dry_run and hasattr(rule, "last_funnel"):
                    rule.last_funnel = funnel_dict
                    rule.last_match_count = 0
                    rule.last_run_at = datetime.now(timezone.utc)
                continue

            # Early skip for trend lookback limit (Q10)
            if has_trend and prev_start < lookback_limit:
                funnel_dict = {
                    "rule_id": rule_id_str,
                    "evaluated_at": datetime.now(timezone.utc).isoformat(),
                    "date_window": date_window_dict,
                    "entities_in_scope": len(campaigns_in_scope),
                    "enabled_campaigns_count": len(enabled_campaigns_in_scope),
                    "conditions": [],
                    "final_candidates": 0,
                    "trend_outside_lookback": True,
                    "skipped_reason": "Trend rule skipped: previous comparison window extends beyond Amazon 60-day reporting look-back limit",
                    "tip": "Shorten the evaluation window so that the previous period falls within the last 60 days.",
                }
                summary_text, config_status_text, tip = build_funnel_summary(funnel_dict, rule, account)
                funnel_dict["summary_text"] = summary_text
                funnel_dict["config_status_text"] = config_status_text
                funnel_dict["tip"] = tip
                funnels[rule_id_str] = funnel_dict
                if hasattr(rule, "id") and rule.id is not None:
                    funnels[rule.id] = funnel_dict
                if not dry_run and hasattr(rule, "last_funnel"):
                    rule.last_funnel = funnel_dict
                    rule.last_match_count = 0
                    rule.last_run_at = datetime.now(timezone.utc)
                continue

            rule_candidates: list[Candidate] = []

            # Funnel stage trackers
            entities_in_scope = 0
            excluded_campaign_not_enabled = 0
            excluded_target_not_enabled = 0
            no_traffic = 0
            below_min_clicks = 0
            conditions_funnel = [
                {
                    "index": idx,
                    "metric": c.get("metric") if isinstance(c, dict) else c.metric,
                    "op": c.get("op") if isinstance(c, dict) else c.op,
                    "threshold": float(c.get("value") if isinstance(c, dict) else c.value),
                    "passed": 0,
                    "failed": 0,
                }
                for idx, c in enumerate(conditions)
            ]
            skipped_bid_unknown = 0
            skipped_already_at_limit = 0
            skipped_no_target_acos = 0
            flagged_active_exact_keyword = 0

            if affected_param == "CAMPAIGN":
                entities_in_scope = len(campaigns_in_scope)
                excluded_campaign_not_enabled = excluded_camp_not_enabled
                excluded_target_not_enabled = 0

                has_oob = any(
                    (cond.get("metric") if isinstance(cond, dict) else getattr(cond, "metric", "")) == "out_of_budget_days"
                    for cond in conditions
                )
                oob_map = {}
                earliest_snapshot_date_str = None
                if has_oob and active_campaign_ids:
                    status_res = await db.execute(
                        select(CampaignStatusDaily.campaign_id, func.count(CampaignStatusDaily.date))
                        .where(
                            CampaignStatusDaily.ads_account_id == account.id,
                            CampaignStatusDaily.campaign_id.in_(active_campaign_ids),
                            CampaignStatusDaily.date >= start,
                            CampaignStatusDaily.date <= end,
                            CampaignStatusDaily.serving_status.ilike("%OUT_OF_BUDGET%"),
                        )
                        .group_by(CampaignStatusDaily.campaign_id)
                    )
                    oob_map = {row[0]: row[1] for row in status_res.all()}
                    earliest_res = await db.execute(
                        select(func.min(CampaignStatusDaily.date)).where(
                            CampaignStatusDaily.ads_account_id == account.id
                        )
                    )
                    earliest_d = earliest_res.scalar()
                    earliest_snapshot_date_str = earliest_d.isoformat() if earliest_d else start.isoformat()

                if campaigns_to_evaluate:
                    stmt = (
                        select(
                            MetricDaily.entity_id.label("campaign_id"),
                            func.sum(MetricDaily.impressions).label("impressions"),
                            func.sum(MetricDaily.clicks).label("clicks"),
                            func.sum(MetricDaily.spend).label("spend"),
                            func.sum(MetricDaily.sales).label("sales"),
                            func.sum(MetricDaily.orders).label("orders"),
                        )
                        .where(
                            MetricDaily.entity_type == "campaign",
                            MetricDaily.date >= start,
                            MetricDaily.date <= end,
                            MetricDaily.entity_id.in_(active_campaign_ids),
                        )
                        .group_by(MetricDaily.entity_id)
                    )
                    rows = (await db.execute(stmt)).all()
                    metrics_by_camp = {r.campaign_id: r for r in rows}

                    prev_metrics_by_camp = {}
                    if has_trend:
                        prev_camp_stmt = (
                            select(
                                MetricDaily.entity_id.label("campaign_id"),
                                func.sum(MetricDaily.impressions).label("impressions"),
                                func.sum(MetricDaily.clicks).label("clicks"),
                                func.sum(MetricDaily.spend).label("spend"),
                                func.sum(MetricDaily.sales).label("sales"),
                                func.sum(MetricDaily.orders).label("orders"),
                            )
                            .where(
                                MetricDaily.entity_type == "campaign",
                                MetricDaily.date >= prev_start,
                                MetricDaily.date <= prev_end,
                                MetricDaily.entity_id.in_(active_campaign_ids),
                            )
                            .group_by(MetricDaily.entity_id)
                        )
                        prev_rows = (await db.execute(prev_camp_stmt)).all()
                        prev_metrics_by_camp = {r.campaign_id: r for r in prev_rows}

                    for c in campaigns_to_evaluate:
                        # Entity Change Guard (Q5: suppress recently modified entities)
                        if ("campaign", c.id) in recently_changed_entities:
                            continue

                        t_acos = target_acos_by_camp.get(c.id)
                        if has_acos_vs_target and t_acos is None:
                            skipped_no_target_acos += 1
                            continue

                        m_row = metrics_by_camp.get(c.id)
                        oob_days = float(oob_map.get(c.id, 0))
                        if not m_row or (
                            (m_row.clicks or 0) == 0
                            and (m_row.impressions or 0) == 0
                            and (m_row.spend or 0) == 0
                        ):
                            if not has_oob:
                                no_traffic += 1
                                continue
                            m = {
                                "impressions": 0.0,
                                "clicks": 0.0,
                                "spend": 0.0,
                                "sales": 0.0,
                                "orders": 0.0,
                                "out_of_budget_days": oob_days,
                            }
                        else:
                            m = {
                                "impressions": float(m_row.impressions or 0),
                                "clicks": float(m_row.clicks or 0),
                                "spend": float(m_row.spend or 0),
                                "sales": float(m_row.sales or 0),
                                "orders": float(m_row.orders or 0),
                                "out_of_budget_days": oob_days,
                            }
                        if m["clicks"] < min_clicks:
                            below_min_clicks += 1
                            continue

                        metrics = derive(m)
                        if has_oob:
                            metrics["out_of_budget_days"] = oob_days

                        # Entity Age (Q11)
                        c_start = c.start_date or (c.created_at.date() if c.created_at else today)
                        metrics["entity_age_days"] = (today - c_start).days

                        # Trend metrics (Q10)
                        if has_trend:
                            prev_r = prev_metrics_by_camp.get(c.id)
                            prev_spend = float(prev_r.spend or 0) if prev_r else 0.0
                            prev_sales = float(prev_r.sales or 0) if prev_r else 0.0
                            prev_orders = float(prev_r.orders or 0) if prev_r else 0.0
                            prev_clicks = float(prev_r.clicks or 0) if prev_r else 0.0
                            prev_impr = float(prev_r.impressions or 0) if prev_r else 0.0

                            prev_acos = (prev_spend / prev_sales * 100.0) if prev_sales > 0 else None
                            prev_ctr = (prev_clicks / prev_impr * 100.0) if prev_impr > 0 else 0.0
                            prev_cvr = (prev_orders / prev_clicks * 100.0) if prev_clicks > 0 else 0.0
                            prev_cpc = (prev_spend / prev_clicks) if prev_clicks > 0 else 0.0

                            metrics["spend_change_pct"] = compute_pct_change(metrics.get("spend"), prev_spend)
                            metrics["sales_change_pct"] = compute_pct_change(metrics.get("sales"), prev_sales)
                            metrics["orders_change_pct"] = compute_pct_change(metrics.get("orders"), prev_orders)
                            metrics["acos_change_pct"] = compute_pct_change(metrics.get("acos"), prev_acos)
                            metrics["ctr_change_pct"] = compute_pct_change(metrics.get("ctr"), prev_ctr)
                            metrics["cvr_change_pct"] = compute_pct_change(metrics.get("cvr"), prev_cvr)
                            metrics["cpc_change_pct"] = compute_pct_change(metrics.get("cpc"), prev_cpc)

                        passed_all = True
                        for idx, cond in enumerate(conditions):
                            cond_match, _ = self._eval_single_condition(cond, metrics, target_acos=t_acos)
                            if not cond_match:
                                conditions_funnel[idx]["failed"] += 1
                                passed_all = False
                                break
                            else:
                                conditions_funnel[idx]["passed"] += 1

                        if not passed_all:
                            continue

                        current_budget = float(c.daily_budget) if c.daily_budget is not None else None
                        if actions and current_budget is None and not is_enable_rule:
                            skipped_bid_unknown += 1
                            continue

                        current_info = {"daily_budget": current_budget, "state": c.state}
                        if actions:
                            suggested = self._build_suggested_actions(
                                actions,
                                "CAMPAIGN",
                                current_info,
                                m_rules,
                                bid_threshold=bt_map.get(c.id),
                            )
                            if not suggested:
                                skipped_already_at_limit += 1
                                continue
                        else:
                            suggested = []

                        matched_conds = self._evaluate_conditions(conditions, metrics, target_acos=t_acos)
                        projection = self._compute_projection(metrics, days)
                        dedupe_key = f"{rule_id_str}:campaign:{c.id}"
                        camp_clicks_sum = int(metrics.get("clicks") or 0)
                        camp_orders_sum = int(metrics.get("orders") or 0)
                        conf = compute_confidence(metrics, conditions, campaign_ref=(camp_clicks_sum, camp_orders_sum))

                        rule_candidates.append(
                            Candidate(
                                rule=rule,
                                entity_type="campaign",
                                entity_id=c.id,
                                entity_label=c.name,
                                campaign_id=c.id,
                                ad_group_id=None,
                                metrics=metrics,
                                matched=matched_conds or [],
                                suggested_actions=suggested,
                                projection=projection,
                                dedupe_key=dedupe_key,
                                current=current_info,
                                date_window=date_window_dict,
                                confidence=conf,
                                tracked_since=earliest_snapshot_date_str if has_oob else None,
                            )
                        )

            elif affected_param == "TARGETING":
                scope_camp_ids = [c.id for c in campaigns_in_scope]
                if scope_camp_ids:
                    kw_stmt = (
                        select(
                            Keyword.id.label("keyword_id"),
                            Keyword.keyword_text,
                            Keyword.match_type,
                            Keyword.bid.label("keyword_bid"),
                            Keyword.state.label("keyword_state"),
                            AdGroup.id.label("ad_group_id"),
                            AdGroup.default_bid.label("ad_group_default_bid"),
                            Campaign.id.label("campaign_id"),
                            Campaign.name.label("campaign_name"),
                            Campaign.state.label("campaign_state"),
                        )
                        .join(AdGroup, Keyword.ad_group_id == AdGroup.id)
                        .join(Campaign, AdGroup.campaign_id == Campaign.id)
                        .where(Campaign.id.in_(scope_camp_ids))
                    )
                    all_kws = (await db.execute(kw_stmt)).all()
                else:
                    all_kws = []

                entities_in_scope = len(all_kws)
                excluded_campaign_not_enabled = sum(1 for kw in all_kws if kw.campaign_state != "ENABLED")
                excluded_target_not_enabled = sum(
                    1 for kw in all_kws if kw.campaign_state == "ENABLED" and kw.keyword_state != "ENABLED"
                )
                eligible_kws = [
                    kw for kw in all_kws if kw.campaign_state == "ENABLED" and kw.keyword_state == "ENABLED"
                ]

                if eligible_kws:
                    metrics_stmt = (
                        select(
                            MetricDaily.entity_id.label("keyword_id"),
                            func.sum(MetricDaily.impressions).label("impressions"),
                            func.sum(MetricDaily.clicks).label("clicks"),
                            func.sum(MetricDaily.spend).label("spend"),
                            func.sum(MetricDaily.sales).label("sales"),
                            func.sum(MetricDaily.orders).label("orders"),
                        )
                        .where(
                            MetricDaily.entity_type == "keyword",
                            MetricDaily.date >= start,
                            MetricDaily.date <= end,
                            MetricDaily.entity_id.in_([kw.keyword_id for kw in eligible_kws]),
                        )
                        .group_by(MetricDaily.entity_id)
                    )
                    m_rows = (await db.execute(metrics_stmt)).all()
                    metrics_by_kw = {r.keyword_id: r for r in m_rows}

                    # Load ad group and campaign reference sums for confidence calculation (G1)
                    ag_ids = list({kw.ad_group_id for kw in eligible_kws if kw.ad_group_id})
                    camp_ids = list({kw.campaign_id for kw in eligible_kws if kw.campaign_id})

                    ag_ref_map: dict[int, tuple[int, int]] = {}
                    if ag_ids:
                        ag_stmt = (
                            select(
                                MetricDaily.entity_id.label("ad_group_id"),
                                func.sum(MetricDaily.clicks).label("clicks"),
                                func.sum(MetricDaily.orders).label("orders"),
                            )
                            .where(
                                MetricDaily.entity_type == "ad_group",
                                MetricDaily.date >= start,
                                MetricDaily.date <= end,
                                MetricDaily.entity_id.in_(ag_ids),
                            )
                            .group_by(MetricDaily.entity_id)
                        )
                        for r in (await db.execute(ag_stmt)).all():
                            ag_ref_map[r.ad_group_id] = (int(r.clicks or 0), int(r.orders or 0))

                        # If ad group not directly in MetricDaily, sum keyword metrics for that ad group
                        kw_ag_sum_stmt = (
                            select(
                                Keyword.ad_group_id,
                                func.sum(MetricDaily.clicks).label("clicks"),
                                func.sum(MetricDaily.orders).label("orders"),
                            )
                            .join(MetricDaily, (MetricDaily.entity_id == Keyword.id) & (MetricDaily.entity_type == "keyword"))
                            .where(
                                MetricDaily.date >= start,
                                MetricDaily.date <= end,
                                Keyword.ad_group_id.in_(ag_ids),
                            )
                            .group_by(Keyword.ad_group_id)
                        )
                        for r in (await db.execute(kw_ag_sum_stmt)).all():
                            if r.ad_group_id and (r.ad_group_id not in ag_ref_map or ag_ref_map[r.ad_group_id][0] == 0):
                                ag_ref_map[r.ad_group_id] = (int(r.clicks or 0), int(r.orders or 0))

                    camp_ref_map: dict[int, tuple[int, int]] = {}
                    camp_metrics_map: dict[int, dict[str, float]] = {}
                    if camp_ids:
                        camp_stmt = (
                            select(
                                MetricDaily.entity_id.label("campaign_id"),
                                func.sum(MetricDaily.clicks).label("clicks"),
                                func.sum(MetricDaily.orders).label("orders"),
                                func.sum(MetricDaily.spend).label("spend"),
                                func.sum(MetricDaily.sales).label("sales"),
                            )
                            .where(
                                MetricDaily.entity_type == "campaign",
                                MetricDaily.date >= start,
                                MetricDaily.date <= end,
                                MetricDaily.entity_id.in_(camp_ids),
                            )
                            .group_by(MetricDaily.entity_id)
                        )
                        for r in (await db.execute(camp_stmt)).all():
                            camp_ref_map[r.campaign_id] = (int(r.clicks or 0), int(r.orders or 0))
                            camp_metrics_map[r.campaign_id] = {
                                "spend": float(r.spend or 0),
                                "sales": float(r.sales or 0),
                            }

                    prev_metrics_by_kw = {}
                    if has_trend:
                        prev_kw_stmt = (
                            select(
                                MetricDaily.entity_id.label("keyword_id"),
                                func.sum(MetricDaily.impressions).label("impressions"),
                                func.sum(MetricDaily.clicks).label("clicks"),
                                func.sum(MetricDaily.spend).label("spend"),
                                func.sum(MetricDaily.sales).label("sales"),
                                func.sum(MetricDaily.orders).label("orders"),
                            )
                            .where(
                                MetricDaily.entity_type == "keyword",
                                MetricDaily.date >= prev_start,
                                MetricDaily.date <= prev_end,
                                MetricDaily.entity_id.in_([kw.keyword_id for kw in eligible_kws]),
                            )
                            .group_by(MetricDaily.entity_id)
                        )
                        for r in (await db.execute(prev_kw_stmt)).all():
                            prev_metrics_by_kw[r.keyword_id] = r

                    for kw in eligible_kws:
                        # Entity Change Guard (Q5: suppress recently modified entities)
                        if ("keyword", kw.keyword_id) in recently_changed_entities or ("target", kw.keyword_id) in recently_changed_entities:
                            continue

                        t_acos = target_acos_by_camp.get(kw.campaign_id)
                        if has_acos_vs_target and t_acos is None:
                            skipped_no_target_acos += 1
                            continue

                        m_row = metrics_by_kw.get(kw.keyword_id)
                        if not m_row or (
                            (m_row.clicks or 0) == 0
                            and (m_row.impressions or 0) == 0
                            and (m_row.spend or 0) == 0
                        ):
                            no_traffic += 1
                            continue
                        m = {
                            "impressions": float(m_row.impressions or 0),
                            "clicks": float(m_row.clicks or 0),
                            "spend": float(m_row.spend or 0),
                            "sales": float(m_row.sales or 0),
                            "orders": float(m_row.orders or 0),
                        }
                        if m["clicks"] < min_clicks:
                            below_min_clicks += 1
                            continue

                        metrics = derive(m)

                        # Entity Age (Q11)
                        c_camp = camp_map.get(kw.campaign_id)
                        c_start = c_camp.start_date if (c_camp and c_camp.start_date) else today
                        metrics["entity_age_days"] = (today - c_start).days

                        # Derived metrics: Target spend & sales share of campaign (Q9)
                        c_data = camp_metrics_map.get(kw.campaign_id, {})
                        c_spend = c_data.get("spend", 0.0)
                        c_sales = c_data.get("sales", 0.0)
                        metrics["target_spend_share"] = round((m["spend"] / c_spend) * 100.0, 2) if c_spend > 0 else None
                        metrics["target_sales_share"] = round((m["sales"] / c_sales) * 100.0, 2) if c_sales > 0 else None

                        # Trend metrics (Q10)
                        if has_trend:
                            prev_kw_row = prev_metrics_by_kw.get(kw.keyword_id)
                            prev_spend = float(prev_kw_row.spend or 0) if prev_kw_row else 0.0
                            prev_sales = float(prev_kw_row.sales or 0) if prev_kw_row else 0.0
                            prev_orders = float(prev_kw_row.orders or 0) if prev_kw_row else 0.0
                            prev_clicks = float(prev_kw_row.clicks or 0) if prev_kw_row else 0.0
                            prev_impr = float(prev_kw_row.impressions or 0) if prev_kw_row else 0.0

                            prev_acos = (prev_spend / prev_sales * 100.0) if prev_sales > 0 else None
                            prev_ctr = (prev_clicks / prev_impr * 100.0) if prev_impr > 0 else 0.0
                            prev_cvr = (prev_orders / prev_clicks * 100.0) if prev_clicks > 0 else 0.0
                            prev_cpc = (prev_spend / prev_clicks) if prev_clicks > 0 else 0.0

                            metrics["spend_change_pct"] = compute_pct_change(metrics.get("spend"), prev_spend)
                            metrics["sales_change_pct"] = compute_pct_change(metrics.get("sales"), prev_sales)
                            metrics["orders_change_pct"] = compute_pct_change(metrics.get("orders"), prev_orders)
                            metrics["acos_change_pct"] = compute_pct_change(metrics.get("acos"), prev_acos)
                            metrics["ctr_change_pct"] = compute_pct_change(metrics.get("ctr"), prev_ctr)
                            metrics["cvr_change_pct"] = compute_pct_change(metrics.get("cvr"), prev_cvr)
                            metrics["cpc_change_pct"] = compute_pct_change(metrics.get("cpc"), prev_cpc)
                        passed_all = True
                        for idx, cond in enumerate(conditions):
                            cond_match, _ = self._eval_single_condition(cond, metrics, target_acos=t_acos)
                            if not cond_match:
                                conditions_funnel[idx]["failed"] += 1
                                passed_all = False
                                break
                            else:
                                conditions_funnel[idx]["passed"] += 1

                        if not passed_all:
                            continue

                        kw_bid = kw.keyword_bid if kw.keyword_bid is not None else kw.ad_group_default_bid
                        if kw_bid is None:
                            skipped_bid_unknown += 1
                            continue

                        current_bid = float(kw_bid)
                        current_info = {"bid": current_bid, "state": kw.keyword_state}
                        suggested = self._build_suggested_actions(
                            actions,
                            "TARGETING",
                            current_info,
                            m_rules,
                            bid_threshold=bt_map.get(kw.campaign_id),
                        )
                        if not suggested:
                            skipped_already_at_limit += 1
                            continue

                        matched_conds = self._evaluate_conditions(conditions, metrics, target_acos=t_acos)
                        projection = self._compute_projection(metrics, days)
                        dedupe_key = f"{rule_id_str}:target:{kw.keyword_id}"
                        conf = compute_confidence(
                            metrics,
                            conditions,
                            ad_group_ref=ag_ref_map.get(kw.ad_group_id),
                            campaign_ref=camp_ref_map.get(kw.campaign_id),
                        )

                        rule_candidates.append(
                            Candidate(
                                rule=rule,
                                entity_type="target",
                                entity_id=kw.keyword_id,
                                entity_label=kw.keyword_text,
                                campaign_id=kw.campaign_id,
                                ad_group_id=kw.ad_group_id,
                                metrics=metrics,
                                matched=matched_conds or [],
                                suggested_actions=suggested,
                                projection=projection,
                                dedupe_key=dedupe_key,
                                current=current_info,
                                date_window=date_window_dict,
                                confidence=conf,
                            )
                        )

            elif affected_param == "SEARCH_TERM":
                scope_camp_ids = [c.id for c in campaigns_in_scope]
                if scope_camp_ids:
                    st_stmt = (
                        select(
                            SearchTerm.campaign_id,
                            SearchTerm.ad_group_id,
                            func.lower(SearchTerm.search_term).label("term"),
                            SearchTerm.keyword_id,
                            Campaign.state.label("campaign_state"),
                            func.sum(SearchTerm.impressions).label("impressions"),
                            func.sum(SearchTerm.clicks).label("clicks"),
                            func.sum(SearchTerm.spend).label("spend"),
                            func.sum(SearchTerm.sales).label("sales"),
                            func.sum(SearchTerm.orders).label("orders"),
                        )
                        .join(Campaign, SearchTerm.campaign_id == Campaign.id)
                        .where(
                            SearchTerm.date >= start,
                            SearchTerm.date <= end,
                            SearchTerm.campaign_id.in_(scope_camp_ids),
                        )
                        .group_by(
                            SearchTerm.campaign_id,
                            SearchTerm.ad_group_id,
                            func.lower(SearchTerm.search_term),
                            SearchTerm.keyword_id,
                            Campaign.state,
                        )
                    )
                    st_rows = (await db.execute(st_stmt)).all()
                else:
                    st_rows = []

                # Group by (campaign_id, ad_group_id, term)
                groups: dict[tuple[int, int, str], dict[str, Any]] = {}
                for row in st_rows:
                    key = (row.campaign_id, row.ad_group_id, row.term)
                    if key not in groups:
                        groups[key] = {
                            "campaign_id": row.campaign_id,
                            "ad_group_id": row.ad_group_id,
                            "term": row.term,
                            "campaign_state": row.campaign_state,
                            "impressions": 0.0,
                            "clicks": 0.0,
                            "spend": 0.0,
                            "sales": 0.0,
                            "orders": 0.0,
                            "source_keyword_id": row.keyword_id,
                            "max_kw_clicks": float(row.clicks or 0),
                        }
                    g = groups[key]
                    clk = float(row.clicks or 0)
                    g["impressions"] += float(row.impressions or 0)
                    g["clicks"] += clk
                    g["spend"] += float(row.spend or 0)
                    g["sales"] += float(row.sales or 0)
                    g["orders"] += float(row.orders or 0)
                    if clk > g["max_kw_clicks"]:
                        g["max_kw_clicks"] = clk
                        g["source_keyword_id"] = row.keyword_id

                entities_in_scope = len(groups)

                # Load reference metrics for confidence calculation (G1)
                st_ag_ids = list({ag_id for (camp_id, ag_id, term) in groups.keys() if ag_id})
                st_camp_ids = list({camp_id for (camp_id, ag_id, term) in groups.keys() if camp_id})

                st_ag_ref_map: dict[int, tuple[int, int]] = {}
                if st_ag_ids:
                    st_ag_stmt = (
                        select(
                            MetricDaily.entity_id.label("ad_group_id"),
                            func.sum(MetricDaily.clicks).label("clicks"),
                            func.sum(MetricDaily.orders).label("orders"),
                        )
                        .where(
                            MetricDaily.entity_type == "ad_group",
                            MetricDaily.date >= start,
                            MetricDaily.date <= end,
                            MetricDaily.entity_id.in_(st_ag_ids),
                        )
                        .group_by(MetricDaily.entity_id)
                    )
                    for r in (await db.execute(st_ag_stmt)).all():
                        st_ag_ref_map[r.ad_group_id] = (int(r.clicks or 0), int(r.orders or 0))

                    kw_st_ag_stmt = (
                        select(
                            Keyword.ad_group_id,
                            func.sum(MetricDaily.clicks).label("clicks"),
                            func.sum(MetricDaily.orders).label("orders"),
                        )
                        .join(MetricDaily, (MetricDaily.entity_id == Keyword.id) & (MetricDaily.entity_type == "keyword"))
                        .where(
                            MetricDaily.date >= start,
                            MetricDaily.date <= end,
                            Keyword.ad_group_id.in_(st_ag_ids),
                        )
                        .group_by(Keyword.ad_group_id)
                    )
                    for r in (await db.execute(kw_st_ag_stmt)).all():
                        if r.ad_group_id and (r.ad_group_id not in st_ag_ref_map or st_ag_ref_map[r.ad_group_id][0] == 0):
                            st_ag_ref_map[r.ad_group_id] = (int(r.clicks or 0), int(r.orders or 0))

                st_camp_ref_map: dict[int, tuple[int, int]] = {}
                st_camp_metrics_map: dict[int, dict[str, float]] = {}
                if st_camp_ids:
                    st_camp_stmt = (
                        select(
                            MetricDaily.entity_id.label("campaign_id"),
                            func.sum(MetricDaily.clicks).label("clicks"),
                            func.sum(MetricDaily.orders).label("orders"),
                            func.sum(MetricDaily.spend).label("spend"),
                            func.sum(MetricDaily.sales).label("sales"),
                        )
                        .where(
                            MetricDaily.entity_type == "campaign",
                            MetricDaily.date >= start,
                            MetricDaily.date <= end,
                            MetricDaily.entity_id.in_(st_camp_ids),
                        )
                        .group_by(MetricDaily.entity_id)
                    )
                    for r in (await db.execute(st_camp_stmt)).all():
                        st_camp_ref_map[r.campaign_id] = (int(r.clicks or 0), int(r.orders or 0))
                        st_camp_metrics_map[r.campaign_id] = {
                            "spend": float(r.spend or 0),
                            "sales": float(r.sales or 0),
                        }

                # Search-Term Safety (Q13) preloading:
                # - never Add as Targeting if term exists as ENABLED kw (same match type) or is already negated
                # - never negate a term that is an ENABLED exact kw in the same ad group (flag instead)
                enabled_exact_kws: set[tuple[int, str]] = set()
                enabled_kws_by_match: set[tuple[int, str, str]] = set()
                if st_ag_ids:
                    kws_stmt = select(Keyword.ad_group_id, Keyword.match_type, Keyword.keyword_text).where(
                        Keyword.ad_group_id.in_(st_ag_ids),
                        Keyword.state == "ENABLED",
                    )
                    for k_row in (await db.execute(kws_stmt)).all():
                        ag_k = k_row.ad_group_id
                        mt_k = (k_row.match_type or "").strip().upper()
                        txt_k = (k_row.keyword_text or "").strip().lower()
                        enabled_kws_by_match.add((ag_k, mt_k, txt_k))
                        if mt_k == "EXACT":
                            enabled_exact_kws.add((ag_k, txt_k))

                negated_terms_camp: set[tuple[int, str]] = set()
                negated_terms_ag: set[tuple[int, str]] = set()
                if st_camp_ids:
                    neg_stmt = select(NegativeKeyword.campaign_id, NegativeKeyword.ad_group_id, NegativeKeyword.keyword_text).where(
                        NegativeKeyword.campaign_id.in_(st_camp_ids)
                    )
                    for n_row in (await db.execute(neg_stmt)).all():
                        c_id = n_row.campaign_id
                        a_id = n_row.ad_group_id
                        n_txt = (n_row.keyword_text or "").strip().lower()
                        if a_id:
                            negated_terms_ag.add((a_id, n_txt))
                        else:
                            negated_terms_camp.add((c_id, n_txt))

                for (camp_id, ag_id, term), g in groups.items():
                    if g["campaign_state"] != "ENABLED":
                        excluded_campaign_not_enabled += 1
                        continue

                    # Entity Change Guard (Q5: suppress recently modified search terms)
                    if ("search_term", g["source_keyword_id"]) in recently_changed_entities:
                        continue

                    t_acos = target_acos_by_camp.get(camp_id)
                    if has_acos_vs_target and t_acos is None:
                        skipped_no_target_acos += 1
                        continue

                    m = {
                        "impressions": g["impressions"],
                        "clicks": g["clicks"],
                        "spend": g["spend"],
                        "sales": g["sales"],
                        "orders": g["orders"],
                    }
                    if m["clicks"] < min_clicks:
                        below_min_clicks += 1
                        continue

                    metrics = derive(m)

                    # Entity Age (Q11)
                    c_camp = camp_map.get(camp_id)
                    c_start = c_camp.start_date if (c_camp and c_camp.start_date) else today
                    metrics["entity_age_days"] = (today - c_start).days

                    # Derived metrics: Target spend & sales share of campaign (Q9)
                    c_data = st_camp_metrics_map.get(camp_id, {})
                    c_spend = c_data.get("spend", 0.0)
                    c_sales = c_data.get("sales", 0.0)
                    metrics["target_spend_share"] = round((m["spend"] / c_spend) * 100.0, 2) if c_spend > 0 else None
                    metrics["target_sales_share"] = round((m["sales"] / c_sales) * 100.0, 2) if c_sales > 0 else None
                    passed_all = True
                    for idx, cond in enumerate(conditions):
                        cond_match, _ = self._eval_single_condition(cond, metrics, target_acos=t_acos)
                        if not cond_match:
                            conditions_funnel[idx]["failed"] += 1
                            passed_all = False
                            break
                        else:
                            conditions_funnel[idx]["passed"] += 1

                    if not passed_all:
                        continue

                    current_info = {}
                    suggested = self._build_suggested_actions(
                        actions,
                        "SEARCH_TERM",
                        current_info,
                        m_rules,
                        term=term,
                        ad_group_id=ag_id,
                        camp_id=camp_id,
                        enabled_exact_kws=enabled_exact_kws,
                        enabled_kws_by_match=enabled_kws_by_match,
                        negated_terms_camp=negated_terms_camp,
                        negated_terms_ag=negated_terms_ag,
                    )
                    if not suggested:
                        skipped_already_at_limit += 1
                        continue

                    has_flagged_negate = any(s.get("type") == "FLAGGED_CANNOT_NEGATE" for s in suggested)
                    if has_flagged_negate:
                        flagged_active_exact_keyword += 1

                    matched_conds = self._evaluate_conditions(conditions, metrics, target_acos=t_acos)
                    projection = self._compute_projection(metrics, days)
                    dedupe_key = f"{rule_id_str}:search_term:{camp_id}:{ag_id}:{term}"
                    conf = compute_confidence(
                        metrics,
                        conditions,
                        ad_group_ref=st_ag_ref_map.get(ag_id),
                        campaign_ref=st_camp_ref_map.get(camp_id),
                    )

                    c_evidence_extra = {}
                    if has_flagged_negate:
                        c_evidence_extra["safety_flag"] = "CANNOT_NEGATE_ACTIVE_EXACT_KEYWORD"
                        c_evidence_extra["safety_warning"] = f"Cannot negate search term '{term}': active EXACT keyword already exists in this ad group"

                    rule_candidates.append(
                        Candidate(
                            rule=rule,
                            entity_type="search_term",
                            entity_id=g["source_keyword_id"],
                            entity_label=term,
                            campaign_id=camp_id,
                            ad_group_id=ag_id,
                            metrics=metrics,
                            matched=matched_conds or [],
                            suggested_actions=suggested,
                            projection=projection,
                            dedupe_key=dedupe_key,
                            current=current_info,
                            date_window=date_window_dict,
                            confidence=conf,
                            safety_flag=c_evidence_extra.get("safety_flag"),
                            safety_warning=c_evidence_extra.get("safety_warning"),
                        )
                    )

            elif affected_param == "PLACEMENT":
                if getattr(settings, "ENABLE_PLACEMENT_RULES", False):
                    entities_in_scope = len(campaigns_in_scope) * 3
                    excluded_campaign_not_enabled = (len(campaigns_in_scope) - len(enabled_campaigns_in_scope)) * 3
                    excluded_target_not_enabled = 0

                    if enabled_campaigns_in_scope:
                        stmt = (
                            select(
                                PlacementMetricDaily.campaign_id,
                                PlacementMetricDaily.placement,
                                func.sum(PlacementMetricDaily.impressions).label("impressions"),
                                func.sum(PlacementMetricDaily.clicks).label("clicks"),
                                func.sum(PlacementMetricDaily.spend).label("spend"),
                                func.sum(PlacementMetricDaily.sales).label("sales"),
                                func.sum(PlacementMetricDaily.orders).label("orders"),
                            )
                            .where(
                                PlacementMetricDaily.date >= start,
                                PlacementMetricDaily.date <= end,
                                PlacementMetricDaily.campaign_id.in_(active_campaign_ids),
                            )
                            .group_by(
                                PlacementMetricDaily.campaign_id,
                                PlacementMetricDaily.placement,
                            )
                        )
                        rows = (await db.execute(stmt)).all()
                        p_by_camp_placement = {(r.campaign_id, r.placement): r for r in rows}

                        placement_labels = {
                            "TOP_OF_SEARCH": "Top of Search",
                            "REST_OF_SEARCH": "Rest of Search",
                            "PRODUCT_PAGES": "Product Pages",
                        }
                        all_placements = ["TOP_OF_SEARCH", "REST_OF_SEARCH", "PRODUCT_PAGES"]

                        for c in enabled_campaigns_in_scope:
                            for placement in all_placements:
                                p_row = p_by_camp_placement.get((c.id, placement))
                                if not p_row or (
                                    (p_row.clicks or 0) == 0
                                    and (p_row.impressions or 0) == 0
                                    and (p_row.spend or 0) == 0
                                ):
                                    no_traffic += 1
                                    continue
                                m = {
                                    "impressions": float(p_row.impressions or 0),
                                    "clicks": float(p_row.clicks or 0),
                                    "spend": float(p_row.spend or 0),
                                    "sales": float(p_row.sales or 0),
                                    "orders": float(p_row.orders or 0),
                                }
                                if m["clicks"] < min_clicks:
                                    below_min_clicks += 1
                                    continue

                                metrics = derive(m)
                                passed_all = True
                                for idx, cond in enumerate(conditions):
                                    cond_match, _ = self._eval_single_condition(cond, metrics)
                                    if not cond_match:
                                        conditions_funnel[idx]["failed"] += 1
                                        passed_all = False
                                        break
                                    else:
                                        conditions_funnel[idx]["passed"] += 1

                                if not passed_all:
                                    continue

                                if placement == "TOP_OF_SEARCH":
                                    current_pct = int(c.placement_top_pct or 0)
                                    field_name = "placement_top_pct"
                                elif placement == "REST_OF_SEARCH":
                                    current_pct = int(c.placement_rest_pct or 0)
                                    field_name = "placement_rest_pct"
                                elif placement == "PRODUCT_PAGES":
                                    current_pct = int(c.placement_product_page_pct or 0)
                                    field_name = "placement_product_page_pct"
                                else:
                                    continue

                                current_info = {
                                    "placement": placement,
                                    "pct": current_pct,
                                    "placement_field": field_name,
                                    "campaign_name": c.name,
                                }
                                suggested = self._build_suggested_actions(
                                    actions, "PLACEMENT", current_info, m_rules, placement=placement
                                )
                                if not suggested:
                                    skipped_already_at_limit += 1
                                    continue

                                matched_conds = self._evaluate_conditions(conditions, metrics)
                                projection = self._compute_projection(metrics, days)
                                dedupe_key = f"{rule_id_str}:placement:{c.id}:{placement}"
                                entity_label = placement_labels.get(placement, placement)

                                rule_candidates.append(
                                    Candidate(
                                        rule=rule,
                                        entity_type="placement",
                                        entity_id=c.id,
                                        entity_label=entity_label,
                                        campaign_id=c.id,
                                        ad_group_id=None,
                                        metrics=metrics,
                                        matched=matched_conds or [],
                                        suggested_actions=suggested,
                                        projection=projection,
                                        dedupe_key=dedupe_key,
                                        current=current_info,
                                        date_window=date_window_dict,
                                    )
                                )

            eval_time = datetime.now(ZoneInfo("Asia/Kolkata"))
            rule_funnel = {
                "entities_in_scope": entities_in_scope,
                "enabled_campaigns_count": len(enabled_campaigns_in_scope),
                "total_campaigns_count": len(campaigns_in_scope),
                "excluded_campaign_not_enabled": excluded_campaign_not_enabled,
                "excluded_target_not_enabled": excluded_target_not_enabled,
                "no_traffic": no_traffic,
                "below_min_clicks": below_min_clicks,
                "conditions": conditions_funnel,
                "skipped_bid_unknown": skipped_bid_unknown,
                "skipped_already_at_limit": skipped_already_at_limit,
                "skipped_no_target_acos": skipped_no_target_acos,
                "flagged_active_exact_keyword": flagged_active_exact_keyword,
                "final_candidates": len(rule_candidates),
                "suppressed_already_open": 0,
                "suppressed_cooldown": 0,
                "suppressed": 0,
                "created_open": len(rule_candidates),
                "date_window": {
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "days": days,
                    "label": date_window_label,
                },
                "evaluated_at": eval_time.isoformat(),
            }
            summary_text, config_status_text, tip = build_funnel_summary(rule_funnel, rule, account)
            rule_funnel["summary_text"] = summary_text
            rule_funnel["config_status_text"] = config_status_text
            rule_funnel["tip"] = tip

            funnels[rule_id_str] = rule_funnel
            if getattr(rule, "id", None) is not None:
                funnels[rule.id] = rule_funnel

            total_evaluated_count += entities_in_scope

            # Rank by spend desc and cap at LIMITS["max_open_per_rule"] (R3.6)
            rule_candidates.sort(key=lambda c: float(c.metrics.get("spend") or 0), reverse=True)
            max_open = LIMITS.get("max_open_per_rule", 200)
            if len(rule_candidates) > max_open:
                rule_funnel["notice"] = f"Showing the {max_open} biggest by spend of {len(rule_candidates)} matching"
            all_candidates.extend(rule_candidates[:max_open])

        self.last_evaluated_count = total_evaluated_count
        self.last_funnels = funnels
        return all_candidates, funnels

    def _eval_single_condition(
        self,
        cond: dict[str, Any] | Any,
        metrics: dict[str, Any],
        target_acos: Optional[float] = None,
    ) -> tuple[bool, Optional[float]]:
        metric_name = cond.get("metric") if isinstance(cond, dict) else cond.metric
        op = cond.get("op") if isinstance(cond, dict) else cond.op
        val_raw = cond.get("value") if isinstance(cond, dict) else cond.value
        threshold = float(val_raw)

        val = metrics.get(metric_name)

        if metric_name == "acos_vs_target":
            if target_acos is None:
                return False, None
            threshold = float(target_acos) + float(val_raw)
            spend = metrics.get("spend") or 0
            sales = metrics.get("sales") or 0
            if spend > 0 and sales == 0:
                eval_val = float("inf")
            elif spend == 0:
                eval_val = None
            else:
                eval_val = metrics.get("acos")
            actual_display = round(float(metrics.get("acos")), 2) if metrics.get("acos") is not None and not math.isinf(eval_val) else None
        elif metric_name == "acos":
            spend = metrics.get("spend") or 0
            sales = metrics.get("sales") or 0
            if spend > 0 and sales == 0:
                eval_val = float("inf")
            elif spend == 0:
                eval_val = None
            else:
                eval_val = val
            actual_display = None
            if val is not None and not math.isinf(eval_val):
                actual_display = round(float(val), 2)
        else:
            eval_val = val
            actual_display = None
            if val is not None:
                actual_display = round(float(val), 2)

        if eval_val is None:
            return False, None

        match = False
        if op == "GT":
            match = eval_val > threshold
        elif op == "GTE":
            match = eval_val >= threshold
        elif op == "LT":
            match = eval_val < threshold
        elif op == "LTE":
            match = eval_val <= threshold
        elif op == "EQ":
            match = abs(eval_val - threshold) < 1e-9

        return match, actual_display

    def _evaluate_conditions(
        self,
        conditions: list[dict[str, Any] | Any],
        metrics: dict[str, Any],
        target_acos: Optional[float] = None,
    ) -> Optional[list[dict[str, Any]]]:
        matched: list[dict[str, Any]] = []
        for cond in conditions:
            match, actual_display = self._eval_single_condition(cond, metrics, target_acos=target_acos)
            if not match:
                return None
            metric_name = cond.get("metric") if isinstance(cond, dict) else cond.metric
            op = cond.get("op") if isinstance(cond, dict) else cond.op
            val_raw = cond.get("value") if isinstance(cond, dict) else cond.value
            item: dict[str, Any] = {
                "metric": metric_name,
                "op": op,
                "threshold": float(val_raw),
                "actual": actual_display,
            }
            if metric_name == "acos_vs_target" and target_acos is not None:
                item["target_acos"] = round(float(target_acos), 2)
                item["offset"] = float(val_raw)
            matched.append(item)
        return matched


    def _compute_projection(self, metrics: dict[str, Any], days: int) -> Optional[dict[str, Any]]:
        orders = float(metrics.get("orders") or 0)
        if orders == 0:
            spend = round(float(metrics.get("spend") or 0), 2)
            monthly_equiv = round(spend / days * 30, 2)
            return {
                "days": days,
                "spend_in_period": spend,
                "monthly_equivalent": monthly_equiv,
            }
        return None

    def _build_suggested_actions(
        self,
        actions: list[dict[str, Any] | Any],
        affected_parameter: str,
        current_info: dict[str, Any],
        m_rules: dict[str, Any],
        term: str = "",
        ad_group_id: Optional[int] = None,
        placement: str = "",
        bid_threshold: Optional[CampaignBidThreshold] = None,
        camp_id: Optional[int] = None,
        enabled_exact_kws: Optional[set[tuple[int, str]]] = None,
        enabled_kws_by_match: Optional[set[tuple[int, str, str]]] = None,
        negated_terms_camp: Optional[set[tuple[int, str]]] = None,
        negated_terms_ag: Optional[set[tuple[int, str]]] = None,
    ) -> list[dict[str, Any]]:
        suggested: list[dict[str, Any]] = []

        for action in actions:
            act_type = action.get("type") if isinstance(action, dict) else action.type
            params = (action.get("params") if isinstance(action, dict) else action.params) or {}
            params = dict(params)

            if act_type == "ADD_AS_NEGATIVE":
                clean_term = term.strip().lower()
                # Q13: never negate a term that is an ENABLED exact keyword in the same ad group (flag instead)
                if enabled_exact_kws and ad_group_id and (ad_group_id, clean_term) in enabled_exact_kws:
                    suggested.append({
                        "type": "FLAGGED_CANNOT_NEGATE",
                        "params": params,
                        "from": None,
                        "to": None,
                        "warning": f"Cannot negate search term '{term}': active EXACT keyword already exists in this ad group",
                    })
                    continue

                match_type = params.get("match_type", "NEGATIVE_EXACT")
                level = params.get("level", "AD_GROUP")
                suggested.append({
                    "type": "ADD_AS_NEGATIVE",
                    "params": params,
                    "from": None,
                    "to": {"match_type": match_type, "level": level, "keyword_text": term},
                })

            elif act_type == "ADD_AS_TARGETING":
                clean_term = term.strip().lower()
                match_type = str(params.get("match_type", "EXACT")).upper()
                # Q13: never Add as Targeting if the term already exists as ENABLED keyword (same match type) or is already negated
                if enabled_kws_by_match and ad_group_id and (ad_group_id, match_type, clean_term) in enabled_kws_by_match:
                    continue
                if negated_terms_ag and ad_group_id and (ad_group_id, clean_term) in negated_terms_ag:
                    continue
                if negated_terms_camp and camp_id and (camp_id, clean_term) in negated_terms_camp:
                    continue

                bid = params.get("bid")
                suggested.append({
                    "type": "ADD_AS_TARGETING",
                    "params": params,
                    "from": None,
                    "to": {"match_type": match_type, "bid": bid, "keyword_text": term},
                })

            elif act_type in ("INCREASE_BID", "DECREASE_BID"):
                raw_bid = current_info.get("bid")
                if raw_bid is None:
                    # Bid is unknown - never invent a current value (F4)
                    return []
                mode = params.get("mode", "percent")
                val = params.get("value", params.get("pct", 10))
                up = (act_type == "INCREASE_BID")
                min_bid = Decimal(str(m_rules["min_bid"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                max_bid = Decimal(str(m_rules["max_bid"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

                # Clamp to campaign's saved CampaignBidThreshold if present (Q6)
                if bid_threshold:
                    if bid_threshold.min_bid is not None:
                        min_bid = max(min_bid, Decimal(str(bid_threshold.min_bid)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
                    if bid_threshold.max_bid is not None:
                        max_bid = min(max_bid, Decimal(str(bid_threshold.max_bid)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

                current_bid, clamped_bid = compute_decimal_bid_change(
                    current_bid=raw_bid,
                    pct=params.get("pct"),
                    up=up,
                    min_bid=min_bid,
                    max_bid=max_bid,
                    mode=mode,
                    value=val,
                )
                if clamped_bid == current_bid:
                    # Clamped at min/max bid -> already at limit (no-op) -> skip
                    return []
                suggested.append({
                    "type": act_type,
                    "params": params,
                    "from": float(current_bid),
                    "to": float(clamped_bid),
                })

            elif act_type == "PAUSE_TARGET":
                current_state = current_info.get("state", "ENABLED")
                if current_state == "PAUSED":
                    return []
                suggested.append({
                    "type": "PAUSE_TARGET",
                    "params": params,
                    "from": current_state,
                    "to": "PAUSED",
                })

            elif act_type in ("INCREASE_BUDGET", "DECREASE_BUDGET"):
                raw_budget = current_info.get("daily_budget")
                if raw_budget is None:
                    # Budget is unknown - skip
                    return []
                mode = params.get("mode", "percent")
                val = params.get("value", params.get("pct", 10))
                up = (act_type == "INCREASE_BUDGET")
                min_budget = Decimal(str(m_rules["min_budget"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                max_budget = Decimal(str(m_rules.get("max_daily_budget", 20000000.0))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                current_budget, clamped_budget = compute_decimal_budget_change(
                    current_budget=raw_budget,
                    pct=params.get("pct"),
                    up=up,
                    min_budget=min_budget,
                    max_budget=max_budget,
                    mode=mode,
                    value=val,
                )
                if clamped_budget == current_budget:
                    # Clamped at min/max budget -> already at limit -> skip
                    return []
                suggested.append({
                    "type": act_type,
                    "params": params,
                    "from": float(current_budget),
                    "to": float(clamped_budget),
                })

            elif act_type == "SET_BUDGET":
                raw_budget = current_info.get("daily_budget")
                if raw_budget is None:
                    return []
                target_val = params.get("value")
                if target_val is None:
                    return []
                min_budget = Decimal(str(m_rules["min_budget"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                max_budget = Decimal(str(m_rules.get("max_daily_budget", 20000000.0))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                curr_budget = Decimal(str(raw_budget)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                target_budget = Decimal(str(target_val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                clamped_target = min(max(target_budget, min_budget), max_budget)
                if clamped_target == curr_budget:
                    # Skip if equal (no-op)
                    return []
                suggested.append({
                    "type": "SET_BUDGET",
                    "params": params,
                    "from": float(curr_budget),
                    "to": float(clamped_target),
                })

            elif act_type == "ENABLE_CAMPAIGN":
                current_state = current_info.get("state", "PAUSED")
                if current_state != "PAUSED":
                    return []
                suggested.append({
                    "type": "ENABLE_CAMPAIGN",
                    "params": params,
                    "from": "PAUSED",
                    "to": "ENABLED",
                })

            elif act_type == "PAUSE_CAMPAIGN":
                current_state = current_info.get("state", "ENABLED")
                if current_state == "PAUSED":
                    return []
                suggested.append({
                    "type": "PAUSE_CAMPAIGN",
                    "params": params,
                    "from": current_state,
                    "to": "PAUSED",
                })

            elif act_type == "INCREASE_PLACEMENT":
                points = int(params.get("points", 10))
                current_pct = int(current_info.get("pct", 0))
                new_pct = min(max(current_pct + points, 0), 900)
                if new_pct == current_pct:
                    return []
                suggested.append({
                    "type": "INCREASE_PLACEMENT",
                    "params": {**params, "placement": placement},
                    "from": current_pct,
                    "to": new_pct,
                })

            elif act_type == "DECREASE_PLACEMENT":
                points = int(params.get("points", 10))
                current_pct = int(current_info.get("pct", 0))
                new_pct = min(max(current_pct - points, 0), 900)
                if new_pct == current_pct:
                    return []
                suggested.append({
                    "type": "DECREASE_PLACEMENT",
                    "params": {**params, "placement": placement},
                    "from": current_pct,
                    "to": new_pct,
                })

        return suggested


ENGINES: dict[str, RecommendationEngine] = {"rules": RuleBasedEngine()}


def get_engine() -> RecommendationEngine:
    engine_name = getattr(settings, "RECOMMENDATION_ENGINE", "rules")
    return ENGINES.get(engine_name, ENGINES["rules"])


async def run_engine(
    db: AsyncSession,
    account: AdsAccount,
    rule_ids: Optional[list[int]] = None,
    trigger: str = "manual",
) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    if not account.is_active or account.connection_status != "connected":
        return {
            "created": 0,
            "expired": 0,
            "skipped": 0,
            "finished_at": started_at.isoformat(),
            "notice": "Account is inactive or disconnected — recommendation generation skipped",
        }
    engine = get_engine()

    # Query active rules for this account
    query = select(RecommendationRule).where(
        RecommendationRule.ads_account_id == account.id,
        RecommendationRule.is_active == True,
    )
    if rule_ids is not None:
        query = query.where(RecommendationRule.id.in_(rule_ids))

    rules_res = await db.execute(query.options(selectinload(RecommendationRule.rule_campaigns)))
    rules = rules_res.scalars().all()

    today = today_for(account.marketplace)

    # Freeze Period Guard (Q12: no new recommendations generated during freeze periods)
    in_freeze, freeze_range = is_in_freeze_period(today)
    if in_freeze:
        return {
            "created": 0,
            "expired": 0,
            "skipped": 0,
            "finished_at": started_at.isoformat(),
            "notice": f"Freeze period active ({freeze_range}) — recommendation generation is paused",
        }

    candidates, funnels = await engine.evaluate(db, account, rules, today=today, dry_run=False)

    candidate_dedupe_keys = {c.dedupe_key for c in candidates}
    evaluated_rule_ids = {r.id for r in rules}

    # Fetch existing recommendations for this account
    existing_recs_res = await db.execute(
        select(Recommendation).where(
            Recommendation.ads_account_id == account.id,
        )
    )
    all_recs = existing_recs_res.scalars().all()

    cooldown_days = LIMITS.get("cooldown_days", 7)
    cooldown_threshold = started_at - timedelta(days=cooldown_days)

    # Map existing recs by dedupe_key
    recs_by_key: dict[str, list[Recommendation]] = {}
    for r in all_recs:
        recs_by_key.setdefault(r.dedupe_key, []).append(r)

    created_count = 0
    skipped_count = 0
    expired_count = 0

    per_rule_suppressed_open: dict[int, int] = {r.id: 0 for r in rules}
    per_rule_suppressed_cooldown: dict[int, int] = {r.id: 0 for r in rules}

    # 1. Insert candidates as OPEN recommendations unless duplicate or within cooldown
    for c in candidates:
        rid = getattr(c.rule, "id", None)
        existing_list = recs_by_key.get(c.dedupe_key, [])
        is_open = any(r.status == "OPEN" for r in existing_list)

        # Snooze Guard (Q17: suppress recommendations while snoozed)
        is_snoozed = any(
            r.evidence
            and isinstance(r.evidence, dict)
            and r.evidence.get("snoozed_until")
            and date.fromisoformat(str(r.evidence["snoozed_until"])[:10]) >= today
            for r in existing_list
        )
        if is_snoozed:
            skipped_count += 1
            continue

        is_in_cooldown = any(
            r.status in ("DISMISSED", "APPLIED")
            and r.resolved_at is not None
            and (
                r.resolved_at >= cooldown_threshold
                if r.resolved_at.tzinfo
                else r.resolved_at.replace(tzinfo=timezone.utc) >= cooldown_threshold
            )
            for r in existing_list
        )

        if is_open:
            skipped_count += 1
            if rid in per_rule_suppressed_open:
                per_rule_suppressed_open[rid] += 1
            continue

        if is_in_cooldown:
            skipped_count += 1
            if rid in per_rule_suppressed_cooldown:
                per_rule_suppressed_cooldown[rid] += 1
            continue

        new_rec = Recommendation(
            ads_account_id=account.id,
            rule_id=c.rule.id,
            rule_name=c.rule.name,
            source="RULE",
            affected_parameter=c.rule.affected_parameter,
            entity_type=c.entity_type,
            entity_id=c.entity_id,
            entity_label=c.entity_label,
            campaign_id=c.campaign_id,
            ad_group_id=c.ad_group_id,
            evidence={
                "metrics": c.metrics,
                "matched": c.matched,
                "current": c.current,
                "projection": c.projection,
                "date_window": c.date_window,
                **({"confidence": c.confidence} if c.confidence else {}),
                **({"tracked_since": c.tracked_since} if c.tracked_since else {}),
                **({"safety_flag": c.safety_flag} if c.safety_flag else {}),
                **({"safety_warning": c.safety_warning} if c.safety_warning else {}),
            },
            suggested_actions=c.suggested_actions,
            status="OPEN",
            dedupe_key=c.dedupe_key,
            created_at=started_at,
        )
        db.add(new_rec)
        recs_by_key.setdefault(c.dedupe_key, []).append(new_rec)
        created_count += 1

    # 2. Mark OPEN recommendations of evaluated rules whose dedupe_key is not in candidates as EXPIRED
    for r in all_recs:
        if r.rule_id in evaluated_rule_ids and r.status == "OPEN":
            if r.dedupe_key not in candidate_dedupe_keys:
                r.status = "EXPIRED"
                r.resolved_at = started_at
                expired_count += 1

    # 3. Update rule.last_run_at and rule.last_match_count
    per_rule_matches: dict[int, int] = {r.id: 0 for r in rules}
    for c in candidates:
        rid = getattr(c.rule, "id", None)
        if rid in per_rule_matches:
            per_rule_matches[rid] += 1

    for r in rules:
        r.last_run_at = started_at
        r.last_match_count = per_rule_matches.get(r.id, 0)
        if r.id in funnels:
            f = funnels[r.id]
            f["suppressed_already_open"] = per_rule_suppressed_open.get(r.id, 0)
            f["suppressed_cooldown"] = per_rule_suppressed_cooldown.get(r.id, 0)
            f["suppressed"] = f["suppressed_already_open"] + f["suppressed_cooldown"]
            f["created_open"] = f["final_candidates"] - f["suppressed"]
            r.last_funnel = f

    # 4. Write RecommendationRun record
    finished_at = datetime.now(timezone.utc)
    run = RecommendationRun(
        ads_account_id=account.id,
        started_at=started_at,
        finished_at=finished_at,
        trigger=trigger,
        rules_evaluated=len(rules),
        created=created_count,
        expired=expired_count,
        skipped=skipped_count,
        status="SUCCESS",
    )
    db.add(run)

    await db.commit()
    await db.refresh(run)

    return {
        "created": created_count,
        "expired": expired_count,
        "skipped": skipped_count,
        "per_rule": [{"rule_id": rid, "matches": cnt, "funnel": funnels.get(rid)} for rid, cnt in per_rule_matches.items()],
        "finished_at": finished_at.isoformat(),
        "run_id": run.id,
    }
