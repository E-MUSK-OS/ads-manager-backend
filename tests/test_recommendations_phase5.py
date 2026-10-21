from datetime import date, datetime, timedelta, timezone
import pytest
from httpx import AsyncClient
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.marketplace_rules import today_for
from app.models.ads_account import AdsAccount
from app.models.campaign import Campaign
from app.models.metric_daily import MetricDaily
from app.models.placement_metric_daily import PlacementMetricDaily
from app.models.recommendation import Recommendation
from app.models.recommendation_rule import RecommendationRule
from app.models.user import User
from app.providers.ads.mock_provider import (
    MockAdsProvider,
    split_campaign_metrics_to_placements,
    backfill_placement_metrics,
)
from app.recommendations.applier import apply_action, apply_recommendation
from app.recommendations.engine import get_engine, run_engine
from app.recommendations.registry import TEMPLATES, resolve_template


@pytest.mark.asyncio
async def test_placement_split_math_reconciliation():
    """Verify largest-remainder and last-remainder math reconciles exactly for various numbers."""
    test_cases = [
        (100, 10, 15.35, 45.00, 2),
        (1, 1, 0.05, 0.00, 0),
        (999, 137, 248.73, 512.44, 7),
        (0, 0, 0.0, 0.0, 0),
        (50, 3, 7.01, 19.99, 1),
    ]

    for imps, clks, spd, sal, ords in test_cases:
        res = split_campaign_metrics_to_placements(imps, clks, spd, sal, ords, noise_seed=42)
        placements = list(res.values())
        assert sum(p["impressions"] for p in placements) == imps
        assert sum(p["clicks"] for p in placements) == clks
        assert round(sum(p["spend"] for p in placements), 2) == round(spd, 2)
        assert round(sum(p["sales"] for p in placements), 2) == round(sal, 2)
        assert sum(p["orders"] for p in placements) == ords


@pytest.mark.asyncio
async def test_placements_reconcile_to_campaign_totals_in_db(
    db_session: AsyncSession, account_a: AdsAccount
):
    """Verify all placement daily rows in DB sum exactly to campaign daily rows."""
    provider = MockAdsProvider(db_session)
    await provider.generate_for_new_connection(account_a.id)

    # Check DB placement rows vs campaign metric rows
    camp_ids = (
        await db_session.execute(
            select(Campaign.id).where(Campaign.ads_account_id == account_a.id)
        )
    ).scalars().all()

    m_rows = (
        await db_session.execute(
            select(MetricDaily).where(
                MetricDaily.entity_type == "campaign",
                MetricDaily.entity_id.in_(camp_ids),
            )
        )
    ).scalars().all()

    assert len(m_rows) > 0

    p_rows = (
        await db_session.execute(
            select(PlacementMetricDaily).where(
                PlacementMetricDaily.campaign_id.in_(camp_ids)
            )
        )
    ).scalars().all()

    # Each campaign-day has exactly 3 placement rows
    assert len(p_rows) == len(m_rows) * 3

    # Group placements by (campaign_id, date)
    p_by_camp_date: dict[tuple[int, date], list[PlacementMetricDaily]] = {}
    for p in p_rows:
        p_by_camp_date.setdefault((p.campaign_id, p.date), []).append(p)

    for m in m_rows:
        plist = p_by_camp_date.get((m.entity_id, m.date), [])
        assert len(plist) == 3
        assert sum(p.impressions for p in plist) == m.impressions
        assert sum(p.clicks for p in plist) == m.clicks
        assert round(sum(p.spend for p in plist), 2) == round(m.spend, 2)
        assert round(sum(p.sales for p in plist), 2) == round(m.sales, 2)
        assert sum(p.orders for p in plist) == m.orders


@pytest.mark.asyncio
async def test_placement_rule_creates_candidates_per_matching_placement(
    db_session: AsyncSession, account_a: AdsAccount
):
    """A PLACEMENT rule generates Candidate items for matching (campaign, placement) pairs."""
    provider = MockAdsProvider(db_session)
    await provider.generate_for_new_connection(account_a.id)

    today = today_for(account_a.marketplace)
    engine = get_engine()

    rule = RecommendationRule(
        id=501,
        ads_account_id=account_a.id,
        name="Boost Top of Search",
        name_key="boost top of search",
        affected_parameter="PLACEMENT",
        date_filter="LAST_30_DAYS",
        min_clicks=5,
        conditions=[{"metric": "spend", "op": "GT", "value": 0.0}],
        actions=[{"type": "INCREASE_PLACEMENT", "params": {"points": 25}}],
        scope_mode="ALL",
        is_active=True,
    )

    candidates = await engine.evaluate(db_session, account_a, [rule], today=today, dry_run=True)
    assert len(candidates) > 0

    for c in candidates:
        assert c.entity_type == "placement"
        assert c.entity_label in ("Top of Search", "Rest of Search", "Product Pages")
        assert c.campaign_id is not None
        assert c.suggested_actions[0]["type"] == "INCREASE_PLACEMENT"
        assert c.suggested_actions[0]["params"]["points"] == 25
        assert c.suggested_actions[0]["to"] == c.current["pct"] + 25
        assert f":placement:{c.campaign_id}:" in c.dedupe_key


@pytest.mark.asyncio
async def test_placement_template_boost_top_of_search(
    db_session: AsyncSession, account_a: AdsAccount
):
    """Verify the default template tpl_boost_top_of_search produces candidates on mock data."""
    provider = MockAdsProvider(db_session)
    await provider.generate_for_new_connection(account_a.id)

    today = today_for(account_a.marketplace)
    engine = get_engine()

    tpl = next((t for t in TEMPLATES if t["key"] == "boost_top_of_search"), None)
    assert tpl is not None
    resolved = resolve_template(tpl, account_a.marketplace)

    rule = RecommendationRule(
        id=502,
        ads_account_id=account_a.id,
        name=resolved["name"],
        name_key=resolved["name"].lower(),
        affected_parameter=resolved["affected_parameter"],
        date_filter=resolved["date_filter"],
        min_clicks=resolved.get("min_clicks", 0),
        conditions=resolved["conditions"],
        actions=resolved["actions"],
        scope_mode="ALL",
        is_active=True,
    )

    candidates = await engine.evaluate(db_session, account_a, [rule], today=today, dry_run=True)
    assert len(candidates) >= 1
    assert any(c.entity_label == "Top of Search" for c in candidates)


@pytest.mark.asyncio
async def test_apply_changes_only_targeted_placement_percentage(
    db_session: AsyncSession, account_a: AdsAccount, user_a: User
):
    """Applying a placement recommendation modifies only that placement's pct and clamps 0-900."""
    camp = Campaign(
        ads_account_id=account_a.id,
        name="Placement Test Campaign",
        external_id="pl_test_c1",
        state="ENABLED",
        daily_budget=50.0,
        placement_top_pct=10,
        placement_rest_pct=20,
        placement_product_page_pct=30,
    )
    db_session.add(camp)
    await db_session.commit()
    await db_session.refresh(camp)

    rule = RecommendationRule(
        ads_account_id=account_a.id,
        name="Placement Rule Top",
        name_key="placement rule top",
        affected_parameter="PLACEMENT",
        date_filter="LAST_30_DAYS",
        min_clicks=0,
        conditions=[{"metric": "spend", "op": "GT", "value": 0.0}],
        actions=[{"type": "INCREASE_PLACEMENT", "params": {"points": 25}}],
        scope_mode="ALL",
    )
    db_session.add(rule)
    await db_session.commit()
    await db_session.refresh(rule)

    rec = Recommendation(
        ads_account_id=account_a.id,
        rule_id=rule.id,
        rule_name=rule.name,
        affected_parameter="PLACEMENT",
        entity_type="placement",
        entity_id=camp.id,
        entity_label="Top of Search",
        campaign_id=camp.id,
        evidence={
            "metrics": {"spend": 100, "clicks": 20},
            "matched": [],
            "current": {"placement": "TOP_OF_SEARCH", "pct": 10},
        },
        suggested_actions=[
            {
                "type": "INCREASE_PLACEMENT",
                "params": {"points": 25, "placement": "TOP_OF_SEARCH"},
                "from": 10,
                "to": 35,
            }
        ],
        status="OPEN",
        dedupe_key=f"{rule.id}:placement:{camp.id}:TOP_OF_SEARCH",
    )
    db_session.add(rec)
    await db_session.commit()
    await db_session.refresh(rec)

    # Apply the recommendation
    applied = await apply_recommendation(db_session, user_a, rec)
    assert applied.status == "APPLIED"

    await db_session.refresh(camp)
    # placement_top_pct changed from 10 to 35
    assert camp.placement_top_pct == 35
    # Other two placement percentages MUST NOT change
    assert camp.placement_rest_pct == 20
    assert camp.placement_product_page_pct == 30


@pytest.mark.asyncio
async def test_apply_placement_clamps_between_0_and_900(
    db_session: AsyncSession, account_a: AdsAccount, user_a: User
):
    """Verify clamping at 900 (upper) and 0 (lower)."""
    camp = Campaign(
        ads_account_id=account_a.id,
        name="Placement Clamp Campaign",
        external_id="pl_clamp_c1",
        state="ENABLED",
        daily_budget=50.0,
        placement_top_pct=880,
        placement_rest_pct=15,
        placement_product_page_pct=0,
    )
    db_session.add(camp)
    await db_session.commit()
    await db_session.refresh(camp)

    rule = RecommendationRule(
        ads_account_id=account_a.id,
        name="Clamp Rule",
        name_key="clamp rule",
        affected_parameter="PLACEMENT",
        date_filter="LAST_30_DAYS",
        min_clicks=0,
        conditions=[{"metric": "spend", "op": "GT", "value": 0.0}],
        actions=[{"type": "INCREASE_PLACEMENT", "params": {"points": 50}}],
        scope_mode="ALL",
    )
    db_session.add(rule)
    await db_session.commit()
    await db_session.refresh(rule)

    # 1. Clamp to 900
    rec_high = Recommendation(
        ads_account_id=account_a.id,
        rule_id=rule.id,
        rule_name=rule.name,
        affected_parameter="PLACEMENT",
        entity_type="placement",
        entity_id=camp.id,
        entity_label="Top of Search",
        campaign_id=camp.id,
        evidence={"current": {"placement": "TOP_OF_SEARCH", "pct": 880}},
        suggested_actions=[
            {
                "type": "INCREASE_PLACEMENT",
                "params": {"points": 50, "placement": "TOP_OF_SEARCH"},
                "from": 880,
                "to": 900,
            }
        ],
        status="OPEN",
        dedupe_key=f"{rule.id}:placement:{camp.id}:top_high",
    )
    db_session.add(rec_high)
    await db_session.commit()
    await apply_recommendation(db_session, user_a, rec_high)
    await db_session.refresh(camp)
    assert camp.placement_top_pct == 900  # Clamped to 900

    # 2. Clamp to 0
    rec_low = Recommendation(
        ads_account_id=account_a.id,
        rule_id=rule.id,
        rule_name=rule.name,
        affected_parameter="PLACEMENT",
        entity_type="placement",
        entity_id=camp.id,
        entity_label="Rest of Search",
        campaign_id=camp.id,
        evidence={"current": {"placement": "REST_OF_SEARCH", "pct": 15}},
        suggested_actions=[
            {
                "type": "DECREASE_PLACEMENT",
                "params": {"points": 50, "placement": "REST_OF_SEARCH"},
                "from": 15,
                "to": 0,
            }
        ],
        status="OPEN",
        dedupe_key=f"{rule.id}:placement:{camp.id}:rest_low",
    )
    db_session.add(rec_low)
    await db_session.commit()
    await apply_recommendation(db_session, user_a, rec_low)
    await db_session.refresh(camp)
    assert camp.placement_rest_pct == 0  # Clamped to 0


@pytest.mark.asyncio
async def test_apply_placement_stale_check_raises_409(
    db_session: AsyncSession, account_a: AdsAccount, user_a: User
):
    """If placement percentage on campaign changed after recommendation creation, apply raises 409."""
    camp = Campaign(
        ads_account_id=account_a.id,
        name="Stale Placement Campaign",
        external_id="pl_stale_c1",
        state="ENABLED",
        daily_budget=50.0,
        placement_top_pct=50,  # Changed to 50!
    )
    db_session.add(camp)
    await db_session.commit()
    await db_session.refresh(camp)

    rule = RecommendationRule(
        ads_account_id=account_a.id,
        name="Stale Rule",
        name_key="stale rule",
        affected_parameter="PLACEMENT",
        date_filter="LAST_30_DAYS",
        min_clicks=0,
        conditions=[{"metric": "spend", "op": "GT", "value": 0.0}],
        actions=[{"type": "INCREASE_PLACEMENT", "params": {"points": 20}}],
        scope_mode="ALL",
    )
    db_session.add(rule)
    await db_session.commit()
    await db_session.refresh(rule)

    rec = Recommendation(
        ads_account_id=account_a.id,
        rule_id=rule.id,
        rule_name=rule.name,
        affected_parameter="PLACEMENT",
        entity_type="placement",
        entity_id=camp.id,
        entity_label="Top of Search",
        campaign_id=camp.id,
        evidence={"current": {"placement": "TOP_OF_SEARCH", "pct": 10}},  # Expected 10, live is 50!
        suggested_actions=[
            {
                "type": "INCREASE_PLACEMENT",
                "params": {"points": 20, "placement": "TOP_OF_SEARCH"},
                "from": 10,
                "to": 30,
            }
        ],
        status="OPEN",
        dedupe_key=f"{rule.id}:placement:{camp.id}:stale",
    )
    db_session.add(rec)
    await db_session.commit()

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await apply_recommendation(db_session, user_a, rec)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_meta_and_crud_for_placement_rule(
    client_a: AsyncClient, account_a: AdsAccount
):
    """Verify GET /meta shows PLACEMENT as available and creating a PLACEMENT rule succeeds."""
    # 1. GET /meta
    meta_res = await client_a.get(f"/recommendations/meta?ads_account_id={account_a.id}")
    assert meta_res.status_code == 200
    meta = meta_res.json()
    assert meta["affected_parameters"]["PLACEMENT"]["available"] is True
    placement_tpl = next((t for t in meta["templates"] if t["affected_parameter"] == "PLACEMENT"), None)
    assert placement_tpl is not None
    assert placement_tpl["available"] is True

    # 2. POST /rules with PLACEMENT
    payload = {
        "name": "Live Placement Rule API",
        "affected_parameter": "PLACEMENT",
        "date_filter": "LAST_30_DAYS",
        "min_clicks": 5,
        "conditions": [{"metric": "spend", "op": "GT", "value": 10}],
        "actions": [{"type": "INCREASE_PLACEMENT", "params": {"points": 20}}],
        "scope_mode": "ALL",
        "campaign_ids": [],
    }
    create_res = await client_a.post(
        f"/recommendations/rules?ads_account_id={account_a.id}",
        json=payload,
    )
    assert create_res.status_code in (200, 201), create_res.text
    created = create_res.json()
    rule_data = created.get("rule", created)
    assert rule_data["affected_parameter"] == "PLACEMENT"
    assert rule_data["name"] == "Live Placement Rule API"
