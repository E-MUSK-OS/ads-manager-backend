from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone, timedelta
import math
from typing import Any, Optional, Protocol

from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.core.marketplace_rules import marketplace_or_us, today_for
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
from app.recommendations.dates import resolve_date_filter
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
        }


class RecommendationEngine(Protocol):
    async def evaluate(
        self,
        db: AsyncSession,
        account: AdsAccount,
        rules: list[Any],
        *,
        today: date,
        dry_run: bool = False,
    ) -> list[Candidate]:
        ...


class RuleBasedEngine:
    def __init__(self) -> None:
        self.last_evaluated_count: int = 0

    async def evaluate(
        self,
        db: AsyncSession,
        account: AdsAccount,
        rules: list[Any],
        *,
        today: date,
        dry_run: bool = False,
    ) -> list[Candidate]:
        all_candidates: list[Candidate] = []
        total_evaluated_count = 0
        m_rules = marketplace_or_us(account.marketplace)

        # Pre-fetch all enabled campaigns for this account
        camp_res = await db.execute(
            select(Campaign).where(
                Campaign.ads_account_id == account.id,
                Campaign.state == "ENABLED",
            )
        )
        all_enabled_campaigns = camp_res.scalars().all()
        enabled_campaigns_by_id = {c.id: c for c in all_enabled_campaigns}

        for rule in rules:
            # Skip inactive rules unless dry_run (like preview) is requested
            if not dry_run and not getattr(rule, "is_active", True):
                continue

            date_filter = getattr(rule, "date_filter", "LAST_30_DAYS")
            start, end = resolve_date_filter(date_filter, today)
            days = max((end - start).days + 1, 1)

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
                    active_campaigns = [c for c in all_enabled_campaigns if c.id in scope_campaign_ids]
                else:  # EXCLUDE
                    active_campaigns = [c for c in all_enabled_campaigns if c.id not in scope_campaign_ids]
            else:  # ALL
                active_campaigns = list(all_enabled_campaigns)

            if not active_campaigns:
                continue

            active_campaign_ids = [c.id for c in active_campaigns]
            camp_map = {c.id: c for c in active_campaigns}
            affected_param = getattr(rule, "affected_parameter", "")
            rule_id_str = str(getattr(rule, "id", "draft"))
            min_clicks = getattr(rule, "min_clicks", 0) or 0
            conditions = getattr(rule, "conditions", []) or []
            actions = getattr(rule, "actions", []) or []

            rule_candidates: list[Candidate] = []

            if affected_param == "CAMPAIGN":
                total_evaluated_count += len(active_campaigns)
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

                for row in rows:
                    c = camp_map.get(row.campaign_id)
                    if not c:
                        continue
                    m = {
                        "impressions": float(row.impressions or 0),
                        "clicks": float(row.clicks or 0),
                        "spend": float(row.spend or 0),
                        "sales": float(row.sales or 0),
                        "orders": float(row.orders or 0),
                    }
                    if m["clicks"] < min_clicks:
                        continue

                    metrics = derive(m)
                    matched_conds = self._evaluate_conditions(conditions, metrics)
                    if matched_conds is None:
                        continue

                    current_budget = float(c.daily_budget or 0)
                    current_info = {"daily_budget": current_budget, "state": c.state}

                    suggested = self._build_suggested_actions(actions, "CAMPAIGN", current_info, m_rules)
                    projection = self._compute_projection(metrics, days)
                    dedupe_key = f"{rule_id_str}:campaign:{c.id}"

                    rule_candidates.append(
                        Candidate(
                            rule=rule,
                            entity_type="campaign",
                            entity_id=c.id,
                            entity_label=c.name,
                            campaign_id=c.id,
                            ad_group_id=None,
                            metrics=metrics,
                            matched=matched_conds,
                            suggested_actions=suggested,
                            projection=projection,
                            dedupe_key=dedupe_key,
                            current=current_info,
                        )
                    )

            elif affected_param == "TARGETING":
                stmt = (
                    select(
                        MetricDaily.entity_id.label("keyword_id"),
                        Keyword.keyword_text,
                        Keyword.match_type,
                        Keyword.bid.label("keyword_bid"),
                        Keyword.state.label("keyword_state"),
                        AdGroup.id.label("ad_group_id"),
                        AdGroup.default_bid.label("ad_group_default_bid"),
                        Campaign.id.label("campaign_id"),
                        Campaign.name.label("campaign_name"),
                        func.sum(MetricDaily.impressions).label("impressions"),
                        func.sum(MetricDaily.clicks).label("clicks"),
                        func.sum(MetricDaily.spend).label("spend"),
                        func.sum(MetricDaily.sales).label("sales"),
                        func.sum(MetricDaily.orders).label("orders"),
                    )
                    .join(Keyword, MetricDaily.entity_id == Keyword.id)
                    .join(AdGroup, Keyword.ad_group_id == AdGroup.id)
                    .join(Campaign, AdGroup.campaign_id == Campaign.id)
                    .where(
                        MetricDaily.entity_type == "keyword",
                        MetricDaily.date >= start,
                        MetricDaily.date <= end,
                        Campaign.id.in_(active_campaign_ids),
                        Keyword.state == "ENABLED",
                    )
                    .group_by(
                        MetricDaily.entity_id,
                        Keyword.keyword_text,
                        Keyword.match_type,
                        Keyword.bid,
                        Keyword.state,
                        AdGroup.id,
                        AdGroup.default_bid,
                        Campaign.id,
                        Campaign.name,
                    )
                )
                rows = (await db.execute(stmt)).all()
                total_evaluated_count += len(rows)

                for row in rows:
                    m = {
                        "impressions": float(row.impressions or 0),
                        "clicks": float(row.clicks or 0),
                        "spend": float(row.spend or 0),
                        "sales": float(row.sales or 0),
                        "orders": float(row.orders or 0),
                    }
                    if m["clicks"] < min_clicks:
                        continue

                    metrics = derive(m)
                    matched_conds = self._evaluate_conditions(conditions, metrics)
                    if matched_conds is None:
                        continue

                    current_bid = float(
                        row.keyword_bid
                        if row.keyword_bid is not None
                        else (
                            row.ad_group_default_bid
                            if row.ad_group_default_bid is not None
                            else m_rules["min_bid"]
                        )
                    )
                    current_info = {"bid": current_bid, "state": row.keyword_state}

                    suggested = self._build_suggested_actions(actions, "TARGETING", current_info, m_rules)
                    projection = self._compute_projection(metrics, days)
                    dedupe_key = f"{rule_id_str}:target:{row.keyword_id}"

                    rule_candidates.append(
                        Candidate(
                            rule=rule,
                            entity_type="target",
                            entity_id=row.keyword_id,
                            entity_label=row.keyword_text,
                            campaign_id=row.campaign_id,
                            ad_group_id=row.ad_group_id,
                            metrics=metrics,
                            matched=matched_conds,
                            suggested_actions=suggested,
                            projection=projection,
                            dedupe_key=dedupe_key,
                            current=current_info,
                        )
                    )

            elif affected_param == "SEARCH_TERM":
                stmt = (
                    select(
                        SearchTerm.campaign_id,
                        SearchTerm.ad_group_id,
                        func.lower(SearchTerm.search_term).label("term"),
                        SearchTerm.keyword_id,
                        func.sum(SearchTerm.impressions).label("impressions"),
                        func.sum(SearchTerm.clicks).label("clicks"),
                        func.sum(SearchTerm.spend).label("spend"),
                        func.sum(SearchTerm.sales).label("sales"),
                        func.sum(SearchTerm.orders).label("orders"),
                    )
                    .where(
                        SearchTerm.date >= start,
                        SearchTerm.date <= end,
                        SearchTerm.campaign_id.in_(active_campaign_ids),
                    )
                    .group_by(
                        SearchTerm.campaign_id,
                        SearchTerm.ad_group_id,
                        func.lower(SearchTerm.search_term),
                        SearchTerm.keyword_id,
                    )
                )
                rows = (await db.execute(stmt)).all()

                # Group by (campaign_id, ad_group_id, term)
                groups: dict[tuple[int, int, str], dict[str, Any]] = {}
                for row in rows:
                    key = (row.campaign_id, row.ad_group_id, row.term)
                    if key not in groups:
                        groups[key] = {
                            "campaign_id": row.campaign_id,
                            "ad_group_id": row.ad_group_id,
                            "term": row.term,
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

                total_evaluated_count += len(groups)

                for (camp_id, ag_id, term), g in groups.items():
                    m = {
                        "impressions": g["impressions"],
                        "clicks": g["clicks"],
                        "spend": g["spend"],
                        "sales": g["sales"],
                        "orders": g["orders"],
                    }
                    if m["clicks"] < min_clicks:
                        continue

                    metrics = derive(m)
                    matched_conds = self._evaluate_conditions(conditions, metrics)
                    if matched_conds is None:
                        continue

                    current_info = {}
                    suggested = self._build_suggested_actions(
                        actions, "SEARCH_TERM", current_info, m_rules, term=term, ad_group_id=ag_id
                    )
                    projection = self._compute_projection(metrics, days)
                    dedupe_key = f"{rule_id_str}:search_term:{camp_id}:{ag_id}:{term}"

                    rule_candidates.append(
                        Candidate(
                            rule=rule,
                            entity_type="search_term",
                            entity_id=g["source_keyword_id"],
                            entity_label=term,
                            campaign_id=camp_id,
                            ad_group_id=ag_id,
                            metrics=metrics,
                            matched=matched_conds,
                            suggested_actions=suggested,
                            projection=projection,
                            dedupe_key=dedupe_key,
                            current=current_info,
                        )
                    )

            elif affected_param == "PLACEMENT":
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
                        PlacementMetricDaily.campaign_id.in_(active_campaign_ids),
                        PlacementMetricDaily.date >= start,
                        PlacementMetricDaily.date <= end,
                    )
                    .group_by(
                        PlacementMetricDaily.campaign_id,
                        PlacementMetricDaily.placement,
                    )
                )
                rows = (await db.execute(stmt)).all()
                total_evaluated_count += len(rows)

                placement_labels = {
                    "TOP_OF_SEARCH": "Top of Search",
                    "REST_OF_SEARCH": "Rest of Search",
                    "PRODUCT_PAGES": "Product Pages",
                }

                for row in rows:
                    c = camp_map.get(row.campaign_id)
                    if not c:
                        continue
                    m = {
                        "impressions": float(row.impressions or 0),
                        "clicks": float(row.clicks or 0),
                        "spend": float(row.spend or 0),
                        "sales": float(row.sales or 0),
                        "orders": float(row.orders or 0),
                    }
                    if m["clicks"] < min_clicks:
                        continue

                    metrics = derive(m)
                    matched_conds = self._evaluate_conditions(conditions, metrics)
                    if matched_conds is None:
                        continue

                    placement = row.placement
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
                            matched=matched_conds,
                            suggested_actions=suggested,
                            projection=projection,
                            dedupe_key=dedupe_key,
                            current=current_info,
                        )
                    )

            # Rank by spend desc and cap at LIMITS["max_open_per_rule"]
            rule_candidates.sort(key=lambda c: float(c.metrics.get("spend") or 0), reverse=True)
            max_open = LIMITS.get("max_open_per_rule", 200)
            all_candidates.extend(rule_candidates[:max_open])

        self.last_evaluated_count = total_evaluated_count
        return all_candidates

    def _evaluate_conditions(
        self,
        conditions: list[dict[str, Any] | Any],
        metrics: dict[str, Any],
    ) -> Optional[list[dict[str, Any]]]:
        matched: list[dict[str, Any]] = []

        for cond in conditions:
            metric_name = cond.get("metric") if isinstance(cond, dict) else cond.metric
            op = cond.get("op") if isinstance(cond, dict) else cond.op
            val_raw = cond.get("value") if isinstance(cond, dict) else cond.value
            threshold = float(val_raw)

            val = metrics.get(metric_name)

            # Override ACOS semantics defined in Part 0:
            # +inf when spend > 0 and sales = 0; None when spend = 0
            if metric_name == "acos":
                spend = metrics.get("spend") or 0
                sales = metrics.get("sales") or 0
                if spend > 0 and sales == 0:
                    eval_val = float("inf")
                elif spend == 0:
                    eval_val = None
                else:
                    eval_val = val
            else:
                eval_val = val

            if eval_val is None:
                return None

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

            if not match:
                return None

            actual_display = None
            if val is not None and not math.isinf(eval_val):
                actual_display = round(float(val), 2)

            matched.append({
                "metric": metric_name,
                "op": op,
                "threshold": threshold,
                "actual": actual_display,
            })

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
    ) -> list[dict[str, Any]]:
        suggested: list[dict[str, Any]] = []

        for action in actions:
            act_type = action.get("type") if isinstance(action, dict) else action.type
            params = (action.get("params") if isinstance(action, dict) else action.params) or {}
            params = dict(params)

            if act_type == "ADD_AS_NEGATIVE":
                match_type = params.get("match_type", "NEGATIVE_EXACT")
                level = params.get("level", "AD_GROUP")
                suggested.append({
                    "type": "ADD_AS_NEGATIVE",
                    "params": params,
                    "from": None,
                    "to": {"match_type": match_type, "level": level, "keyword_text": term},
                })

            elif act_type == "ADD_AS_TARGETING":
                match_type = params.get("match_type", "EXACT")
                bid = params.get("bid")
                suggested.append({
                    "type": "ADD_AS_TARGETING",
                    "params": params,
                    "from": None,
                    "to": {"match_type": match_type, "bid": bid, "keyword_text": term},
                })

            elif act_type == "INCREASE_BID":
                pct = float(params.get("pct", 10))
                current_bid = float(current_info.get("bid") or m_rules["min_bid"])
                new_bid = round(min(max(current_bid * (1 + pct / 100), m_rules["min_bid"]), m_rules["max_bid"]), 2)
                suggested.append({
                    "type": "INCREASE_BID",
                    "params": params,
                    "from": round(current_bid, 2),
                    "to": new_bid,
                })

            elif act_type == "DECREASE_BID":
                pct = float(params.get("pct", 10))
                current_bid = float(current_info.get("bid") or m_rules["min_bid"])
                new_bid = round(min(max(current_bid * (1 - pct / 100), m_rules["min_bid"]), m_rules["max_bid"]), 2)
                suggested.append({
                    "type": "DECREASE_BID",
                    "params": params,
                    "from": round(current_bid, 2),
                    "to": new_bid,
                })

            elif act_type == "PAUSE_TARGET":
                suggested.append({
                    "type": "PAUSE_TARGET",
                    "params": params,
                    "from": current_info.get("state", "ENABLED"),
                    "to": "PAUSED",
                })

            elif act_type == "INCREASE_BUDGET":
                pct = float(params.get("pct", 10))
                current_budget = float(current_info.get("daily_budget") or m_rules["min_budget"])
                new_budget = round(max(current_budget * (1 + pct / 100), m_rules["min_budget"]), 2)
                suggested.append({
                    "type": "INCREASE_BUDGET",
                    "params": params,
                    "from": round(current_budget, 2),
                    "to": new_budget,
                })

            elif act_type == "DECREASE_BUDGET":
                pct = float(params.get("pct", 10))
                current_budget = float(current_info.get("daily_budget") or m_rules["min_budget"])
                new_budget = round(max(current_budget * (1 - pct / 100), m_rules["min_budget"]), 2)
                suggested.append({
                    "type": "DECREASE_BUDGET",
                    "params": params,
                    "from": round(current_budget, 2),
                    "to": new_budget,
                })

            elif act_type == "PAUSE_CAMPAIGN":
                suggested.append({
                    "type": "PAUSE_CAMPAIGN",
                    "params": params,
                    "from": current_info.get("state", "ENABLED"),
                    "to": "PAUSED",
                })

            elif act_type == "INCREASE_PLACEMENT":
                points = int(params.get("points", 10))
                current_pct = int(current_info.get("pct", 0))
                new_pct = min(max(current_pct + points, 0), 900)
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
    candidates = await engine.evaluate(db, account, rules, today=today, dry_run=False)

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

    # 1. Insert candidates as OPEN recommendations unless duplicate or within cooldown
    for c in candidates:
        existing_list = recs_by_key.get(c.dedupe_key, [])
        is_open = any(r.status == "OPEN" for r in existing_list)
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

        if is_open or is_in_cooldown:
            skipped_count += 1
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
        "per_rule": [{"rule_id": rid, "matches": cnt} for rid, cnt in per_rule_matches.items()],
        "finished_at": finished_at.isoformat(),
        "run_id": run.id,
    }
