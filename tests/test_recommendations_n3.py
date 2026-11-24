from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.marketplace_rules import today_for
from app.models.ad_group import AdGroup
from app.models.ads_account import AdsAccount
from app.models.campaign import Campaign
from app.models.campaign_bid_threshold import CampaignBidThreshold
from app.models.campaign_history import CampaignHistory
from app.models.keyword import Keyword
from app.models.metric_daily import MetricDaily
from app.models.recommendation import Recommendation
from app.models.recommendation_rule import RecommendationRule
from app.models.user import User
from app.recommendations.engine import get_engine, compute_pct_change
from app.recommendations.applier import check_apply_guardrails, apply_batch_recommendations


@pytest.mark.asyncio
async def test_q7_data_freshness_guard(db_session: AsyncSession, account_a: AdsAccount):
    """Q7: last successful sync older than 24 hours blocks apply with 409."""
    now = datetime.now(timezone.utc)

    # 1. Fresh sync (2 hours ago) -> passes
    account_a.last_synced_at = now - timedelta(hours=2)
    await db_session.commit()
    await check_apply_guardrails(db_session, account_a)  # Should not raise

    # 2. Stale sync (26 hours ago) -> raises 409
    account_a.last_synced_at = now - timedelta(hours=26)
    await db_session.commit()

    with pytest.raises(Exception) as exc_info:
        await check_apply_guardrails(db_session, account_a)
    assert exc_info.value.status_code == 409
    assert "Data is stale" in exc_info.value.detail


@pytest.mark.asyncio
async def test_q6_bid_threshold_clamping_and_no_op_skip(
    db_session: AsyncSession, account_a: AdsAccount
):
    """Q6: Clamp bid suggestions to campaign saved Bid Threshold and skip if no-op."""
    today = today_for(account_a.marketplace)
    engine = get_engine()

    camp = Campaign(
        ads_account_id=account_a.id,
        name="Camp Threshold Test",
        state="ENABLED",
        targeting_type="MANUAL",
        campaign_type="SPONSORED_PRODUCTS",
        daily_budget=200.0,
        start_date=today - timedelta(days=10),
    )
    db_session.add(camp)
    await db_session.flush()

    ag = AdGroup(campaign_id=camp.id, name="AG Threshold", default_bid=15.0)
    db_session.add(ag)
    await db_session.flush()

    # Keyword bid is 25.0
    kw = Keyword(
        ad_group_id=ag.id,
        keyword_text="bluetooth speaker",
        match_type="EXACT",
        state="ENABLED",
        bid=25.0,
    )
    db_session.add(kw)
    await db_session.flush()

    # Campaign has bid threshold max_bid = 28.0
    bt = CampaignBidThreshold(campaign_id=camp.id, min_bid=5.0, max_bid=28.0)
    db_session.add(bt)

    # Keyword metric: high clicks and high spend
    m1 = MetricDaily(
        entity_type="keyword",
        entity_id=kw.id,
        date=today - timedelta(days=2),
        impressions=1000,
        clicks=50,
        spend=500.0,
        sales=2000.0,
        orders=10,
    )
    # Campaign metric for completeness
    m_camp = MetricDaily(
        entity_type="campaign",
        entity_id=camp.id,
        date=today - timedelta(days=2),
        impressions=1000,
        clicks=50,
        spend=500.0,
        sales=2000.0,
        orders=10,
    )
    db_session.add_all([m1, m_camp])
    await db_session.commit()

    # Rule: INCREASE_BID by 20% (25.0 * 1.2 = 30.0, but clamped to 28.0)
    rule = RecommendationRule(
        ads_account_id=account_a.id,
        name="Boost Bid Test",
        name_key="boost_bid_test",
        affected_parameter="TARGETING",
        date_filter="LAST_7_DAYS",
        min_clicks=10,
        conditions=[{"metric": "clicks", "op": "GTE", "value": 10.0}],
        actions=[{"type": "INCREASE_BID", "params": {"mode": "percent", "value": 20}}],
        scope_mode="ALL",
        is_active=True,
    )

    candidates, funnels = await engine.evaluate(db_session, account_a, [rule], today=today, dry_run=True)
    assert len(candidates) == 1
    # Clamped to max_bid 28.0 instead of 30.0!
    assert candidates[0].suggested_actions[0]["to"] == 28.0

    # Now if keyword bid is already 28.0 -> increasing by 20% clamps to 28.0 -> no-op -> skipped!
    kw.bid = 28.0
    await db_session.commit()

    candidates2, funnels2 = await engine.evaluate(db_session, account_a, [rule], today=today, dry_run=True)
    assert len(candidates2) == 0


@pytest.mark.asyncio
async def test_q5_entity_change_guard(db_session: AsyncSession, account_a: AdsAccount):
    """Q5: Entity change guard suppresses entities changed in CampaignHistory within N days."""
    today = today_for(account_a.marketplace)
    engine = get_engine()

    camp = Campaign(
        ads_account_id=account_a.id,
        name="Camp Guard Test",
        state="ENABLED",
        daily_budget=200.0,
        start_date=today - timedelta(days=10),
    )
    db_session.add(camp)
    await db_session.flush()

    ag = AdGroup(campaign_id=camp.id, name="AG Guard", default_bid=15.0)
    db_session.add(ag)
    await db_session.flush()

    kw = Keyword(
        ad_group_id=ag.id,
        keyword_text="running shoes",
        match_type="EXACT",
        state="ENABLED",
        bid=15.0,
    )
    db_session.add(kw)
    await db_session.flush()

    m1 = MetricDaily(
        entity_type="keyword",
        entity_id=kw.id,
        date=today - timedelta(days=2),
        impressions=500,
        clicks=25,
        spend=150.0,
        sales=600.0,
        orders=3,
    )
    m_camp = MetricDaily(
        entity_type="campaign",
        entity_id=camp.id,
        date=today - timedelta(days=2),
        impressions=500,
        clicks=25,
        spend=150.0,
        sales=600.0,
        orders=3,
    )
    db_session.add_all([m1, m_camp])

    # Record a change in CampaignHistory 2 days ago
    hist = CampaignHistory(
        campaign_id=camp.id,
        event_type="RECOMMENDATION_APPLIED",
        event_title="Recent Bid Change",
        entity_type="keyword",
        entity_id=kw.id,
        created_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    db_session.add(hist)
    await db_session.commit()

    rule = RecommendationRule(
        ads_account_id=account_a.id,
        name="Target Bid Rule",
        name_key="target_bid_rule",
        affected_parameter="TARGETING",
        date_filter="LAST_7_DAYS",
        min_clicks=5,
        conditions=[{"metric": "clicks", "op": "GTE", "value": 5.0}],
        actions=[{"type": "INCREASE_BID", "params": {"mode": "percent", "value": 10}}],
        scope_mode="ALL",
        is_active=True,
    )

    # 1. By default, ENTITY_CHANGE_GUARD_DAYS is None (OFF) -> candidate produced
    settings.ENTITY_CHANGE_GUARD_DAYS = None
    cands, _ = await engine.evaluate(db_session, account_a, [rule], today=today, dry_run=True)
    assert len(cands) == 1

    # 2. When turned ON (e.g. 7 days) -> candidate is suppressed
    settings.ENTITY_CHANGE_GUARD_DAYS = 7
    cands_guarded, _ = await engine.evaluate(db_session, account_a, [rule], today=today, dry_run=True)
    assert len(cands_guarded) == 0

    # Cleanup setting
    settings.ENTITY_CHANGE_GUARD_DAYS = None


@pytest.mark.asyncio
async def test_q8_data_completeness_guard_skips_conversion_rules(
    db_session: AsyncSession, account_a: AdsAccount
):
    """Q8: Date completeness guard skips conversion rules when metric rows are missing in window."""
    today = today_for(account_a.marketplace)
    engine = get_engine()

    camp = Campaign(
        ads_account_id=account_a.id,
        name="Camp Completeness Test",
        state="ENABLED",
        daily_budget=200.0,
        start_date=today - timedelta(days=15),
    )
    db_session.add(camp)
    await db_session.flush()

    ag = AdGroup(campaign_id=camp.id, name="AG Completeness", default_bid=15.0)
    db_session.add(ag)
    await db_session.flush()

    kw = Keyword(
        ad_group_id=ag.id,
        keyword_text="yoga pants",
        match_type="EXACT",
        state="ENABLED",
        bid=15.0,
    )
    db_session.add(kw)
    await db_session.flush()

    # Only populate 1 day of metric rows for a 7-day window -> 6 missing days!
    m1 = MetricDaily(
        entity_type="keyword",
        entity_id=kw.id,
        date=today - timedelta(days=2),
        impressions=200,
        clicks=20,
        spend=100.0,
        sales=500.0,
        orders=4,
    )
    m_camp = MetricDaily(
        entity_type="campaign",
        entity_id=camp.id,
        date=today - timedelta(days=2),
        impressions=200,
        clicks=20,
        spend=100.0,
        sales=500.0,
        orders=4,
    )
    db_session.add_all([m1, m_camp])
    await db_session.commit()

    # Rule with conversion metric (orders) -> must be skipped
    conv_rule = RecommendationRule(
        ads_account_id=account_a.id,
        name="Conversion Rule Test",
        name_key="conversion_rule_test",
        affected_parameter="TARGETING",
        date_filter="LAST_7_DAYS",
        min_clicks=5,
        conditions=[{"metric": "orders", "op": "GTE", "value": 1.0}],
        actions=[{"type": "INCREASE_BID", "params": {"mode": "percent", "value": 10}}],
        scope_mode="ALL",
        is_active=True,
    )

    cands, funnels = await engine.evaluate(db_session, account_a, [conv_rule], today=today, dry_run=True)
    assert len(cands) == 0
    funnel = list(funnels.values())[0]
    assert "missing_data_days" in funnel
    assert len(funnel["missing_data_days"]) > 0
    assert "Conversion rule skipped" in funnel["skipped_reason"]


@pytest.mark.asyncio
async def test_q9_derived_metrics_target_share(db_session: AsyncSession, account_a: AdsAccount):
    """Q9: Target spend share and sales share are correctly calculated from sums."""
    today = today_for(account_a.marketplace)
    engine = get_engine()

    camp = Campaign(
        ads_account_id=account_a.id,
        name="Camp Share Test",
        state="ENABLED",
        daily_budget=500.0,
        start_date=today - timedelta(days=10),
    )
    db_session.add(camp)
    await db_session.flush()

    ag = AdGroup(campaign_id=camp.id, name="AG Share", default_bid=10.0)
    db_session.add(ag)
    await db_session.flush()

    kw = Keyword(
        ad_group_id=ag.id,
        keyword_text="heavy target",
        match_type="EXACT",
        state="ENABLED",
        bid=10.0,
    )
    db_session.add(kw)
    await db_session.flush()

    # Campaign total spend = 1000, Target spend = 400 (40% share)
    # Target sales = 800, Campaign sales = 2000 (40% share)
    date_val = today - timedelta(days=1)
    m_camp = MetricDaily(
        entity_type="campaign",
        entity_id=camp.id,
        date=date_val,
        impressions=2000,
        clicks=200,
        spend=1000.0,
        sales=2000.0,
        orders=40,
    )
    m_kw = MetricDaily(
        entity_type="keyword",
        entity_id=kw.id,
        date=date_val,
        impressions=800,
        clicks=80,
        spend=400.0,
        sales=800.0,
        orders=15,
    )
    db_session.add_all([m_camp, m_kw])
    await db_session.commit()

    # Rule: target_spend_share >= 30% -> matches (40% >= 30%)
    share_rule = RecommendationRule(
        ads_account_id=account_a.id,
        name="High Spend Share Rule",
        name_key="high_spend_share_rule",
        affected_parameter="TARGETING",
        date_filter="YESTERDAY",
        min_clicks=10,
        conditions=[{"metric": "target_spend_share", "op": "GTE", "value": 30.0}],
        actions=[{"type": "DECREASE_BID", "params": {"mode": "percent", "value": 10}}],
        scope_mode="ALL",
        is_active=True,
    )

    cands, _ = await engine.evaluate(db_session, account_a, [share_rule], today=today, dry_run=True)
    assert len(cands) == 1
    assert cands[0].metrics["target_spend_share"] == 40.0
    assert cands[0].metrics["target_sales_share"] == 40.0


@pytest.mark.asyncio
async def test_q10_trend_conditions_and_lookback(db_session: AsyncSession, account_a: AdsAccount):
    """Q10: Trend condition computes % change vs previous period; skips if outside lookback."""
    today = today_for(account_a.marketplace)
    engine = get_engine()

    camp = Campaign(
        ads_account_id=account_a.id,
        name="Camp Trend Test",
        state="ENABLED",
        daily_budget=200.0,
        start_date=today - timedelta(days=20),
    )
    db_session.add(camp)
    await db_session.flush()

    # LAST_7_DAYS ends at T-1
    # Current period: T-7 to T-1
    # Previous period: T-14 to T-8
    curr_date = today - timedelta(days=3)
    prev_date = today - timedelta(days=10)

    # Current spend = 300, Previous spend = 150 -> spend_change_pct = +100.0%
    m_curr = MetricDaily(
        entity_type="campaign",
        entity_id=camp.id,
        date=curr_date,
        impressions=1000,
        clicks=100,
        spend=300.0,
        sales=600.0,
        orders=10,
    )
    m_prev = MetricDaily(
        entity_type="campaign",
        entity_id=camp.id,
        date=prev_date,
        impressions=500,
        clicks=50,
        spend=150.0,
        sales=300.0,
        orders=5,
    )
    db_session.add_all([m_curr, m_prev])
    await db_session.commit()

    trend_rule = RecommendationRule(
        ads_account_id=account_a.id,
        name="Spike in Spend Rule",
        name_key="spike_in_spend_rule",
        affected_parameter="CAMPAIGN",
        date_filter="LAST_7_DAYS",
        min_clicks=10,
        conditions=[{"metric": "spend_change_pct", "op": "GTE", "value": 50.0}],
        actions=[{"type": "DECREASE_BUDGET", "params": {"mode": "percent", "value": 15}}],
        scope_mode="ALL",
        is_active=True,
    )

    cands, _ = await engine.evaluate(db_session, account_a, [trend_rule], today=today, dry_run=True)
    assert len(cands) == 1
    assert cands[0].metrics["spend_change_pct"] == 100.0


@pytest.mark.asyncio
async def test_q18_caps_enforced_in_applier(
    client_a: AsyncClient, db_session: AsyncSession, account_a: AdsAccount, user_a: User
):
    """Q18: Server caps for max bid change % and max daily budget increase."""
    today = today_for(account_a.marketplace)

    camp = Campaign(
        ads_account_id=account_a.id,
        name="Camp Cap Test",
        state="ENABLED",
        daily_budget=100.0,
        start_date=today - timedelta(days=5),
    )
    db_session.add(camp)
    await db_session.flush()

    ag = AdGroup(campaign_id=camp.id, name="AG Cap", default_bid=10.0)
    db_session.add(ag)
    await db_session.flush()

    kw = Keyword(
        ad_group_id=ag.id,
        keyword_text="cap keyword",
        match_type="EXACT",
        state="ENABLED",
        bid=10.0,
    )
    db_session.add(kw)
    await db_session.flush()

    # Rec trying to increase bid from 10 to 20 (+100%)
    rec_bid = Recommendation(
        ads_account_id=account_a.id,
        rule_name="Huge Bid Increase",
        affected_parameter="TARGETING",
        entity_type="target",
        entity_id=kw.id,
        entity_label="cap keyword",
        campaign_id=camp.id,
        ad_group_id=ag.id,
        evidence={"metrics": {}, "matched": [], "current": {"bid": 10.0, "state": "ENABLED"}},
        suggested_actions=[{"type": "INCREASE_BID", "params": {"mode": "amount", "value": 10.0}, "from": 10.0, "to": 20.0}],
        status="OPEN",
        dedupe_key="cap:1",
    )
    db_session.add(rec_bid)
    await db_session.commit()

    # 1. When MAX_BID_CHANGE_PCT_CAP is set to 50%, a 100% bid increase fails
    settings.MAX_BID_CHANGE_PCT_CAP = 50.0
    res = await apply_batch_recommendations(db_session, user_a, [rec_bid])
    assert len(res) == 1
    assert res[0]["success"] is False
    assert "exceeds maximum allowed limit of 50.0%" in res[0]["error"]

    # 2. When cap is None (default), it succeeds
    settings.MAX_BID_CHANGE_PCT_CAP = None
    res2 = await apply_batch_recommendations(db_session, user_a, [rec_bid])
    assert len(res2) == 1
    assert res2[0]["success"] is True

    # 3. Budget increase cap
    rec_budget = Recommendation(
        ads_account_id=account_a.id,
        rule_name="Huge Budget Increase",
        affected_parameter="CAMPAIGN",
        entity_type="campaign",
        entity_id=camp.id,
        entity_label="Camp Cap Test",
        campaign_id=camp.id,
        evidence={"metrics": {}, "matched": [], "current": {"daily_budget": 100.0, "state": "ENABLED"}},
        suggested_actions=[{"type": "SET_BUDGET", "params": {"value": 500.0}, "from": 100.0, "to": 500.0}],
        status="OPEN",
        dedupe_key="cap:2",
    )
    db_session.add(rec_budget)
    await db_session.commit()

    # Cap set to 200 increase. Increase here is 500 - 100 = 400 -> fails
    settings.MAX_DAILY_BUDGET_INCREASE_CAP = 200.0
    res3 = await apply_batch_recommendations(db_session, user_a, [rec_budget])
    assert len(res3) == 1
    assert res3[0]["success"] is False
    assert "exceeds maximum allowed limit of ₹200.0" in res3[0]["error"]

    # Cleanup
    settings.MAX_DAILY_BUDGET_INCREASE_CAP = None
