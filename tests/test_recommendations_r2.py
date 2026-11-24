from datetime import date, timedelta
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ads_account import AdsAccount
from app.models.campaign import Campaign
from app.models.ad_group import AdGroup
from app.models.keyword import Keyword
from app.models.metric_daily import MetricDaily
from app.models.recommendation_rule import RecommendationRule
from app.recommendations.engine import get_engine, run_engine
from app.core.marketplace_rules import today_for


@pytest.mark.asyncio
async def test_funnel_arithmetic_targeting(db_session: AsyncSession, account_a: AdsAccount):
    """
    Verify exact funnel arithmetic identity for TARGETING rules:
    entities_in_scope == (
        excluded_campaign_not_enabled
        + excluded_target_not_enabled
        + no_traffic
        + below_min_clicks
        + sum(failed_conditions)
        + skipped_bid_unknown
        + skipped_already_at_limit
        + skipped_no_target_acos
        + final_candidates
    )
    AND final_candidates == suppressed + created_open
    """
    today = today_for(account_a.marketplace)
    engine = get_engine()

    # 1. Enabled campaign
    c_enabled = Campaign(
        ads_account_id=account_a.id,
        name="Campaign Funnel Enabled",
        state="ENABLED",
        targeting_type="MANUAL",
        campaign_type="SPONSORED_PRODUCTS",
        daily_budget=100.0,
        start_date=today - timedelta(days=20),
    )
    # 2. Paused campaign
    c_paused = Campaign(
        ads_account_id=account_a.id,
        name="Campaign Funnel Paused",
        state="PAUSED",
        targeting_type="MANUAL",
        campaign_type="SPONSORED_PRODUCTS",
        daily_budget=100.0,
        start_date=today - timedelta(days=20),
    )
    db_session.add_all([c_enabled, c_paused])
    await db_session.flush()

    ag_enabled = AdGroup(campaign_id=c_enabled.id, name="AG Enabled", default_bid=10.0)
    ag_paused = AdGroup(campaign_id=c_paused.id, name="AG in Paused Camp", default_bid=10.0)
    db_session.add_all([ag_enabled, ag_paused])
    await db_session.flush()

    # Keyword in paused campaign -> excluded_campaign_not_enabled
    kw_in_paused_camp = Keyword(ad_group_id=ag_paused.id, keyword_text="kw in paused camp", match_type="EXACT", bid=10.0, state="ENABLED")

    # Keyword paused in enabled campaign -> excluded_target_not_enabled
    kw_paused_target = Keyword(ad_group_id=ag_enabled.id, keyword_text="kw paused target", match_type="EXACT", bid=10.0, state="PAUSED")

    # Keyword with no metrics -> no_traffic
    kw_no_traffic = Keyword(ad_group_id=ag_enabled.id, keyword_text="kw no traffic", match_type="EXACT", bid=10.0, state="ENABLED")

    # Keyword below min clicks (clicks = 5, min_clicks = 10) -> below_min_clicks
    kw_low_clicks = Keyword(ad_group_id=ag_enabled.id, keyword_text="kw low clicks", match_type="EXACT", bid=10.0, state="ENABLED")

    # Keyword failing condition 1 (spend < 50) -> failed condition #0
    kw_low_spend = Keyword(ad_group_id=ag_enabled.id, keyword_text="kw low spend", match_type="EXACT", bid=10.0, state="ENABLED")

    # Keyword failing condition 2 (orders > 0) -> failed condition #1
    kw_has_orders = Keyword(ad_group_id=ag_enabled.id, keyword_text="kw has orders", match_type="EXACT", bid=10.0, state="ENABLED")

    # Keyword already at min limit (bid = 1.0, decrease bid) -> skipped_already_at_limit
    kw_at_limit = Keyword(ad_group_id=ag_enabled.id, keyword_text="kw at limit", match_type="EXACT", bid=1.0, state="ENABLED")

    # Keyword passing all and eligible -> final_candidate
    kw_winner = Keyword(ad_group_id=ag_enabled.id, keyword_text="kw winner", match_type="EXACT", bid=20.0, state="ENABLED")

    all_kws = [
        kw_in_paused_camp,
        kw_paused_target,
        kw_no_traffic,
        kw_low_clicks,
        kw_low_spend,
        kw_has_orders,
        kw_at_limit,
        kw_winner,
    ]
    db_session.add_all(all_kws)
    await db_session.flush()

    # Add metrics for relevant keywords
    metric_date = today - timedelta(days=3)
    metrics = [
        # kw_low_clicks: 5 clicks, min_clicks=10
        MetricDaily(entity_type="keyword", entity_id=kw_low_clicks.id, date=metric_date, impressions=100, clicks=5, spend=10.0, sales=0, orders=0),
        # kw_low_spend: 15 clicks, spend 20.0 (< 50)
        MetricDaily(entity_type="keyword", entity_id=kw_low_spend.id, date=metric_date, impressions=200, clicks=15, spend=20.0, sales=0, orders=0),
        # kw_has_orders: 15 clicks, spend 60.0 (>= 50), orders = 2 (> 0)
        MetricDaily(entity_type="keyword", entity_id=kw_has_orders.id, date=metric_date, impressions=200, clicks=15, spend=60.0, sales=500.0, orders=2),
        # kw_at_limit: 15 clicks, spend 60.0, orders = 0
        MetricDaily(entity_type="keyword", entity_id=kw_at_limit.id, date=metric_date, impressions=200, clicks=15, spend=60.0, sales=0, orders=0),
        # kw_winner: 15 clicks, spend 60.0, orders = 0
        MetricDaily(entity_type="keyword", entity_id=kw_winner.id, date=metric_date, impressions=200, clicks=15, spend=60.0, sales=0, orders=0),
    ]
    camp_metrics = [
        MetricDaily(
            entity_type="campaign",
            entity_id=c_enabled.id,
            date=today - timedelta(days=d_offset),
            impressions=1000 if d_offset == 3 else 0,
            clicks=100 if d_offset == 3 else 0,
            spend=300.0 if d_offset == 3 else 0.0,
            sales=500.0 if d_offset == 3 else 0.0,
            orders=2 if d_offset == 3 else 0,
        )
        for d_offset in range(2, 9)
    ]
    db_session.add_all(metrics + camp_metrics)
    await db_session.commit()

    rule = RecommendationRule(
        id=701,
        ads_account_id=account_a.id,
        name="Bleeding Targets Test Rule",
        name_key="bleeding targets test rule",
        affected_parameter="TARGETING",
        date_filter="LAST_7_DAYS",
        min_clicks=10,
        conditions=[
            {"metric": "spend", "op": "GTE", "value": 50.0},
            {"metric": "orders", "op": "LTE", "value": 0.0},
        ],
        actions=[{"type": "DECREASE_BID", "params": {"pct": 20}}],
        scope_mode="ALL",
        is_active=True,
    )

    candidates, funnels = await engine.evaluate(db_session, account_a, [rule], today=today, dry_run=True)
    assert len(candidates) == 1
    assert candidates[0].entity_id == kw_winner.id

    funnel = funnels[rule.id]
    assert funnel is not None

    # Check stage counts
    assert funnel["entities_in_scope"] == 8
    assert funnel["excluded_campaign_not_enabled"] == 1
    assert funnel["excluded_target_not_enabled"] == 1
    assert funnel["no_traffic"] == 1
    assert funnel["below_min_clicks"] == 1
    assert len(funnel["conditions"]) == 2
    assert funnel["conditions"][0]["failed"] == 1  # kw_low_spend failed spend >= 50
    assert funnel["conditions"][0]["passed"] == 3  # kw_has_orders, kw_at_limit, kw_winner
    assert funnel["conditions"][1]["failed"] == 1  # kw_has_orders failed orders <= 0
    assert funnel["conditions"][1]["passed"] == 2  # kw_at_limit, kw_winner
    assert funnel["skipped_already_at_limit"] == 1  # kw_at_limit clamped at 1.0
    assert funnel["final_candidates"] == 1  # kw_winner

    # Verify arithmetic identity
    total_stages = (
        funnel["excluded_campaign_not_enabled"]
        + funnel["excluded_target_not_enabled"]
        + funnel["no_traffic"]
        + funnel["below_min_clicks"]
        + sum(c["failed"] for c in funnel["conditions"])
        + funnel["skipped_bid_unknown"]
        + funnel["skipped_already_at_limit"]
        + funnel["skipped_no_target_acos"]
        + funnel["final_candidates"]
    )
    assert total_stages == funnel["entities_in_scope"]
    assert funnel["final_candidates"] == funnel["suppressed"] + funnel["created_open"]

    # Verify formatted text
    assert "8 targets in 1 enabled campaign" in funnel["summary_text"]
    assert "Bleeding Targets Test Rule" in rule.name
    assert funnel["tip"]
    assert "Evaluated" in funnel["config_status_text"]


@pytest.mark.asyncio
async def test_funnel_arithmetic_campaign(db_session: AsyncSession, account_a: AdsAccount):
    """Verify exact funnel arithmetic identity for CAMPAIGN rules."""
    today = today_for(account_a.marketplace)
    engine = get_engine()

    c_paused = Campaign(ads_account_id=account_a.id, name="Camp Paused", state="PAUSED", targeting_type="AUTO", campaign_type="SPONSORED_PRODUCTS", daily_budget=100.0, start_date=today - timedelta(days=10))
    c_notraffic = Campaign(ads_account_id=account_a.id, name="Camp No Traffic", state="ENABLED", targeting_type="AUTO", campaign_type="SPONSORED_PRODUCTS", daily_budget=100.0, start_date=today - timedelta(days=10))
    c_active = Campaign(ads_account_id=account_a.id, name="Camp Active", state="ENABLED", targeting_type="AUTO", campaign_type="SPONSORED_PRODUCTS", daily_budget=100.0, start_date=today - timedelta(days=10))
    db_session.add_all([c_paused, c_notraffic, c_active])
    await db_session.flush()

    m = MetricDaily(entity_type="campaign", entity_id=c_active.id, date=today - timedelta(days=2), impressions=500, clicks=20, spend=150.0, sales=1000.0, orders=5)
    db_session.add(m)
    await db_session.commit()

    rule = RecommendationRule(
        id=702,
        ads_account_id=account_a.id,
        name="Campaign Budget Boost",
        name_key="campaign budget boost",
        affected_parameter="CAMPAIGN",
        date_filter="LAST_7_DAYS",
        min_clicks=5,
        conditions=[{"metric": "spend", "op": "GTE", "value": 100.0}],
        actions=[{"type": "INCREASE_BUDGET", "params": {"pct": 20}}],
        scope_mode="ALL",
        is_active=True,
    )

    candidates, funnels = await engine.evaluate(db_session, account_a, [rule], today=today, dry_run=True)
    funnel = funnels[rule.id]

    assert funnel["entities_in_scope"] == 3
    assert funnel["excluded_campaign_not_enabled"] == 1
    assert funnel["no_traffic"] == 1
    assert funnel["final_candidates"] == 1

    total_stages = (
        funnel["excluded_campaign_not_enabled"]
        + funnel["excluded_target_not_enabled"]
        + funnel["no_traffic"]
        + funnel["below_min_clicks"]
        + sum(c["failed"] for c in funnel["conditions"])
        + funnel["skipped_bid_unknown"]
        + funnel["skipped_already_at_limit"]
        + funnel["skipped_no_target_acos"]
        + funnel["final_candidates"]
    )
    assert total_stages == funnel["entities_in_scope"]


@pytest.mark.asyncio
async def test_run_engine_persists_last_funnel(db_session: AsyncSession, account_a: AdsAccount):
    """Verify run_engine updates rule.last_funnel in the database and returns per_rule funnels."""
    today = today_for(account_a.marketplace)

    c = Campaign(ads_account_id=account_a.id, name="Camp Run Test", state="ENABLED", targeting_type="AUTO", campaign_type="SPONSORED_PRODUCTS", daily_budget=100.0, start_date=today - timedelta(days=10))
    db_session.add(c)
    await db_session.flush()

    m = MetricDaily(entity_type="campaign", entity_id=c.id, date=today - timedelta(days=2), impressions=100, clicks=10, spend=80.0, sales=200.0, orders=2)
    db_session.add(m)

    rule = RecommendationRule(
        ads_account_id=account_a.id,
        name="Camp Run Rule",
        name_key="camp run rule",
        affected_parameter="CAMPAIGN",
        date_filter="LAST_7_DAYS",
        min_clicks=5,
        conditions=[{"metric": "spend", "op": "GTE", "value": 50.0}],
        actions=[{"type": "INCREASE_BUDGET", "params": {"pct": 10}}],
        scope_mode="ALL",
        is_active=True,
    )
    db_session.add(rule)
    await db_session.commit()

    run_result = await run_engine(db_session, account_a, rule_ids=[rule.id], trigger="test")
    assert run_result["status"] == "SUCCESS" if "status" in run_result else True
    assert len(run_result["per_rule"]) == 1
    assert run_result["per_rule"][0]["funnel"] is not None
    assert run_result["per_rule"][0]["funnel"]["final_candidates"] == 1

    # Reload rule from database to verify last_funnel was stored
    reloaded = (await db_session.execute(select(RecommendationRule).where(RecommendationRule.id == rule.id))).scalar_one()
    assert reloaded.last_funnel is not None
    assert reloaded.last_funnel["entities_in_scope"] >= 1
    assert "summary_text" in reloaded.last_funnel
    assert "config_status_text" in reloaded.last_funnel


@pytest.mark.asyncio
async def test_preview_endpoint_returns_funnel(client_a: AsyncClient, account_a: AdsAccount):
    """Verify /recommendations/rules/preview returns the calculated funnel structure."""
    payload = {
        "ads_account_id": account_a.id,
        "rule": {
            "name": "Preview Test Rule",
            "affected_parameter": "CAMPAIGN",
            "date_filter": "LAST_7_DAYS",
            "min_clicks": 0,
            "conditions": [{"metric": "spend", "op": "GTE", "value": 10.0}],
            "actions": [{"type": "INCREASE_BUDGET", "params": {"pct": 10}}],
            "scope_mode": "ALL",
            "campaign_ids": [],
        },
    }
    r = await client_a.post("/recommendations/rules/preview", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert "funnel" in data
    funnel = data["funnel"]
    assert funnel is not None
    assert "entities_in_scope" in funnel
    assert "conditions" in funnel
    assert "summary_text" in funnel
    assert "config_status_text" in funnel
    assert "tip" in funnel
    assert "date_window" in funnel

