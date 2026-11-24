import pytest
from datetime import date, timedelta
from decimal import Decimal
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.marketplace_rules import today_for
from app.models.ads_account import AdsAccount
from app.models.campaign import Campaign
from app.models.ad_group import AdGroup
from app.models.keyword import Keyword
from app.models.metric_daily import MetricDaily
from app.models.recommendation import Recommendation
from app.models.recommendation_rule import RecommendationRule
from app.models.user import User
from app.recommendations.engine import (
    get_engine,
    compute_decimal_bid_change,
    compute_decimal_budget_change,
)
from app.recommendations.applier import apply_batch_recommendations
from app.recommendations.schemas import RuleIn, validate_rule_in
from app.core.exceptions import ServiceValidationError


@pytest.mark.asyncio
async def test_meta_endpoint_relabels_purchases_and_sales(
    client_a: AsyncClient, account_a: AdsAccount
):
    """Verify /meta endpoint returns 'Purchases (7d)' and 'Sales (7d)' labels."""
    resp = await client_a.get(f"/recommendations/meta?ads_account_id={account_a.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["metrics"]["orders"]["label"] == "Purchases (7d)"
    assert data["metrics"]["sales"]["label"] == "Sales (7d)"
    assert "SET_BUDGET" in data["actions"]["CAMPAIGN"]
    assert "ENABLE_CAMPAIGN" in data["actions"]["CAMPAIGN"]


@pytest.mark.asyncio
async def test_one_action_validator_for_targeting_and_campaign(
    db_session: AsyncSession, account_a: AdsAccount
):
    """Verify TARGETING and CAMPAIGN rules reject > 1 action with validation error."""
    # Multi-action targeting rule
    rule_targeting_multi = RuleIn(
        name="Multi Action Targeting",
        affected_parameter="TARGETING",
        date_filter="LAST_30_DAYS",
        conditions=[{"metric": "spend", "op": "GT", "value": 20.0}],
        actions=[
            {"type": "INCREASE_BID", "params": {"mode": "percent", "value": 10}},
            {"type": "DECREASE_BID", "params": {"mode": "percent", "value": 10}},
        ],
    )
    with pytest.raises(ServiceValidationError) as exc:
        await validate_rule_in(db_session, account_a, rule_targeting_multi)
    assert any("exactly one action" in e["message"] for e in exc.value.errors)

    # Multi-action campaign rule
    rule_camp_multi = RuleIn(
        name="Multi Action Campaign",
        affected_parameter="CAMPAIGN",
        date_filter="LAST_30_DAYS",
        conditions=[{"metric": "spend", "op": "GT", "value": 100.0}],
        actions=[
            {"type": "INCREASE_BUDGET", "params": {"mode": "percent", "value": 10}},
            {"type": "DECREASE_BUDGET", "params": {"mode": "percent", "value": 10}},
        ],
    )
    with pytest.raises(ServiceValidationError) as exc:
        await validate_rule_in(db_session, account_a, rule_camp_multi)
    assert any("exactly one action" in e["message"] for e in exc.value.errors)


@pytest.mark.asyncio
async def test_out_of_budget_display_only_validator(
    db_session: AsyncSession, account_a: AdsAccount
):
    """Verify out_of_budget_days is restricted to CAMPAIGN, 1-14 days, and 0 actions."""
    # 1. Non-CAMPAIGN rule with out_of_budget_days -> fails
    rule_kw_oob = RuleIn(
        name="Targeting OOB Rule",
        affected_parameter="TARGETING",
        date_filter="LAST_30_DAYS",
        conditions=[{"metric": "out_of_budget_days", "op": "GTE", "value": 3.0}],
        actions=[],
    )
    with pytest.raises(ServiceValidationError) as exc:
        await validate_rule_in(db_session, account_a, rule_kw_oob)
    assert any("only allowed for CAMPAIGN rules" in e["message"] for e in exc.value.errors)

    # 2. CAMPAIGN rule with out_of_budget_days and actions -> fails (display only)
    rule_camp_oob_with_act = RuleIn(
        name="Camp OOB with Action",
        affected_parameter="CAMPAIGN",
        date_filter="LAST_30_DAYS",
        conditions=[{"metric": "out_of_budget_days", "op": "GTE", "value": 3.0}],
        actions=[{"type": "INCREASE_BUDGET", "params": {"mode": "percent", "value": 10}}],
    )
    with pytest.raises(ServiceValidationError) as exc:
        await validate_rule_in(db_session, account_a, rule_camp_oob_with_act)
    assert any("display-only" in e["message"] for e in exc.value.errors)

    # 3. CAMPAIGN rule with out_of_budget_days value > 14 -> fails
    rule_camp_oob_high = RuleIn(
        name="Camp OOB High",
        affected_parameter="CAMPAIGN",
        date_filter="LAST_30_DAYS",
        conditions=[{"metric": "out_of_budget_days", "op": "GTE", "value": 20.0}],
        actions=[],
    )
    with pytest.raises(ServiceValidationError) as exc:
        await validate_rule_in(db_session, account_a, rule_camp_oob_high)
    assert any("between 1 and 14" in e["message"] for e in exc.value.errors)

    # 4. Valid display-only CAMPAIGN rule -> passes
    rule_valid_oob = RuleIn(
        name="Valid Camp OOB",
        affected_parameter="CAMPAIGN",
        date_filter="LAST_30_DAYS",
        conditions=[{"metric": "out_of_budget_days", "op": "GTE", "value": 5.0}],
        actions=[],
    )
    await validate_rule_in(db_session, account_a, rule_valid_oob)


@pytest.mark.asyncio
async def test_action_params_mode_percent_and_amount(
    db_session: AsyncSession, account_a: AdsAccount
):
    """Verify mode='percent' and mode='amount', plus backwards compat with old 'pct'."""
    # 1. Amount increase bid
    curr, clamped = compute_decimal_bid_change(
        current_bid=10.0,
        up=True,
        min_bid=Decimal("1.00"),
        max_bid=Decimal("5000.00"),
        mode="amount",
        value=2.50,
    )
    assert curr == Decimal("10.00")
    assert clamped == Decimal("12.50")

    # 2. Amount decrease bid
    curr, clamped = compute_decimal_bid_change(
        current_bid=10.0,
        up=False,
        min_bid=Decimal("1.00"),
        max_bid=Decimal("5000.00"),
        mode="amount",
        value=3.00,
    )
    assert curr == Decimal("10.00")
    assert clamped == Decimal("7.00")

    # 3. Backwards compat old pct
    curr, clamped = compute_decimal_bid_change(
        current_bid=20.0,
        pct=10,
        up=True,
        min_bid=Decimal("1.00"),
        max_bid=Decimal("5000.00"),
    )
    assert curr == Decimal("20.00")
    assert clamped == Decimal("22.00")

    # 4. Amount budget increase
    curr_b, clamped_b = compute_decimal_budget_change(
        current_budget=150.0,
        up=True,
        min_budget=Decimal("100.00"),
        max_budget=Decimal("20000000.00"),
        mode="amount",
        value=50.0,
    )
    assert curr_b == Decimal("150.00")
    assert clamped_b == Decimal("200.00")


@pytest.mark.asyncio
async def test_set_budget_validation_and_engine(
    db_session: AsyncSession, account_a: AdsAccount
):
    """Verify SET_BUDGET enforces >= 100 and skips no-op in engine."""
    # 1. Validation rejects budget < 100
    rule_invalid = RuleIn(
        name="Low Set Budget",
        affected_parameter="CAMPAIGN",
        date_filter="LAST_30_DAYS",
        conditions=[{"metric": "spend", "op": "GT", "value": 50.0}],
        actions=[{"type": "SET_BUDGET", "params": {"value": 50.0}}],
    )
    with pytest.raises(ServiceValidationError) as exc:
        await validate_rule_in(db_session, account_a, rule_invalid)
    assert any("at least ₹100" in e["message"] for e in exc.value.errors)

    # 2. Engine evaluates SET_BUDGET
    today = today_for(account_a.marketplace)
    camp = Campaign(
        ads_account_id=account_a.id,
        name="Camp for Set Budget",
        state="ENABLED",
        targeting_type="MANUAL",
        campaign_type="SPONSORED_PRODUCTS",
        daily_budget=200.0,
        start_date=today - timedelta(days=20),
    )
    db_session.add(camp)
    await db_session.flush()

    # Metric
    db_session.add(
        MetricDaily(
            entity_type="campaign",
            entity_id=camp.id,
            date=today - timedelta(days=5),
            spend=150.0,
            impressions=1000,
            clicks=50,
            orders=5,
            sales=1000.0,
        )
    )
    await db_session.commit()

    # Rule targeting 350 budget
    rule_set_budget = RecommendationRule(
        ads_account_id=account_a.id,
        name="Scale to 350",
        name_key="scale to 350",
        affected_parameter="CAMPAIGN",
        date_filter="LAST_30_DAYS",
        min_clicks=0,
        conditions=[{"metric": "spend", "op": "GT", "value": 50.0}],
        actions=[{"type": "SET_BUDGET", "params": {"value": 350.0}}],
        scope_mode="ALL",
    )
    db_session.add(rule_set_budget)
    await db_session.commit()

    engine = get_engine()
    candidates, _ = await engine.evaluate(db_session, account_a, [rule_set_budget], today=today, dry_run=True)
    assert len(candidates) == 1
    action = candidates[0].suggested_actions[0]
    assert action["type"] == "SET_BUDGET"
    assert action["from"] == 200.0
    assert action["to"] == 350.0

    # Rule targeting 200 budget (equal to current budget) -> no-op skipped
    rule_set_budget_equal = RecommendationRule(
        ads_account_id=account_a.id,
        name="Keep at 200",
        name_key="keep at 200",
        affected_parameter="CAMPAIGN",
        date_filter="LAST_30_DAYS",
        min_clicks=0,
        conditions=[{"metric": "spend", "op": "GT", "value": 50.0}],
        actions=[{"type": "SET_BUDGET", "params": {"value": 200.0}}],
        scope_mode="ALL",
    )
    db_session.add(rule_set_budget_equal)
    await db_session.commit()

    candidates_equal, _ = await engine.evaluate(db_session, account_a, [rule_set_budget_equal], today=today, dry_run=True)
    assert len(candidates_equal) == 0  # Skipped because current == to


@pytest.mark.asyncio
async def test_enable_campaign_evaluates_paused_only(
    db_session: AsyncSession, account_a: AdsAccount, user_a: User
):
    """Verify ENABLE_CAMPAIGN evaluates PAUSED campaigns only, never ARCHIVED."""
    today = today_for(account_a.marketplace)

    # 1. Enabled campaign (should NOT trigger ENABLE_CAMPAIGN)
    c_enabled = Campaign(
        ads_account_id=account_a.id,
        name="Enabled Campaign",
        state="ENABLED",
        targeting_type="MANUAL",
        campaign_type="SPONSORED_PRODUCTS",
        daily_budget=150.0,
        start_date=today - timedelta(days=20),
    )
    # 2. Paused campaign (SHOULD trigger ENABLE_CAMPAIGN)
    c_paused = Campaign(
        ads_account_id=account_a.id,
        name="Paused Campaign to Enable",
        state="PAUSED",
        targeting_type="MANUAL",
        campaign_type="SPONSORED_PRODUCTS",
        daily_budget=200.0,
        start_date=today - timedelta(days=20),
    )
    # 3. Archived campaign (should NEVER trigger)
    c_archived = Campaign(
        ads_account_id=account_a.id,
        name="Archived Campaign",
        state="ARCHIVED",
        targeting_type="MANUAL",
        campaign_type="SPONSORED_PRODUCTS",
        daily_budget=100.0,
        start_date=today - timedelta(days=20),
    )
    db_session.add_all([c_enabled, c_paused, c_archived])
    await db_session.flush()

    # Add past metrics for all three
    for c in [c_enabled, c_paused, c_archived]:
        db_session.add(
            MetricDaily(
                entity_type="campaign",
                entity_id=c.id,
                date=today - timedelta(days=10),
                spend=100.0,
                impressions=1000,
                clicks=40,
                orders=4,
                sales=800.0,
            )
        )
    await db_session.commit()

    rule_enable = RecommendationRule(
        ads_account_id=account_a.id,
        name="Re-enable Good Campaigns",
        name_key="re-enable good campaigns",
        affected_parameter="CAMPAIGN",
        date_filter="LAST_30_DAYS",
        min_clicks=0,
        conditions=[{"metric": "spend", "op": "GT", "value": 50.0}],
        actions=[{"type": "ENABLE_CAMPAIGN", "params": {}}],
        scope_mode="ALL",
    )
    db_session.add(rule_enable)
    await db_session.commit()

    engine = get_engine()
    candidates, _ = await engine.evaluate(db_session, account_a, [rule_enable], today=today, dry_run=True)

    # Should only generate candidate for c_paused
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.entity_id == c_paused.id
    assert cand.suggested_actions[0]["type"] == "ENABLE_CAMPAIGN"
    assert cand.suggested_actions[0]["from"] == "PAUSED"
    assert cand.suggested_actions[0]["to"] == "ENABLED"

    # Now verify applying ENABLE_CAMPAIGN sets state to ENABLED
    rec = Recommendation(
        ads_account_id=account_a.id,
        rule_id=rule_enable.id,
        rule_name=rule_enable.name,
        affected_parameter="CAMPAIGN",
        entity_type="campaign",
        entity_id=c_paused.id,
        entity_label=c_paused.name,
        campaign_id=c_paused.id,
        evidence={
            "metrics": cand.metrics,
            "matched": cand.matched,
            "current": cand.current,
        },
        suggested_actions=cand.suggested_actions,
        status="OPEN",
        dedupe_key=cand.dedupe_key,
    )
    db_session.add(rec)
    await db_session.commit()
    await db_session.refresh(rec)

    results = await apply_batch_recommendations(db_session, user_a, [rec])
    assert len(results) == 1
    assert results[0]["success"] is True

    await db_session.refresh(c_paused)
    assert c_paused.state == "ENABLED"
