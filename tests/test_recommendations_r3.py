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
from app.models.campaign_bid_threshold import CampaignBidThreshold
from app.models.campaign_status_daily import CampaignStatusDaily
from app.models.recommendation import Recommendation
from app.models.recommendation_rule import RecommendationRule
from app.recommendations.dates import resolve_date_filter
from app.recommendations.engine import get_engine, compute_confidence
from app.recommendations.conflicts import are_conflicting_recommendations
from app.core.marketplace_rules import today_for


def test_date_windows_delay():
    """
    R3.1: Date windows end per Q3.
    Traffic-only ends at T-1 (yesterday).
    Conversion metrics end at T-2 (2 days ago).
    """
    t = date(2026, 11, 20)

    # Traffic only: ends yesterday (T-1 = 2026-11-19)
    s_7, e_7 = resolve_date_filter("LAST_7_DAYS", t, has_conversion_metrics=False)
    assert e_7 == date(2026, 11, 19)
    assert s_7 == date(2026, 11, 13)
    assert (e_7 - s_7).days + 1 == 7

    s_30, e_30 = resolve_date_filter("LAST_30_DAYS", t, has_conversion_metrics=False)
    assert e_30 == date(2026, 11, 19)
    assert s_30 == date(2026, 10, 21)
    assert (e_30 - s_30).days + 1 == 30

    # Conversion metrics: ends T-2 (2026-11-18)
    s_7_c, e_7_c = resolve_date_filter("LAST_7_DAYS", t, has_conversion_metrics=True)
    assert e_7_c == date(2026, 11, 18)
    assert s_7_c == date(2026, 11, 12)
    assert (e_7_c - s_7_c).days + 1 == 7

    s_30_c, e_30_c = resolve_date_filter("LAST_30_DAYS", t, has_conversion_metrics=True)
    assert e_30_c == date(2026, 11, 18)
    assert s_30_c == date(2026, 10, 20)
    assert (e_30_c - s_30_c).days + 1 == 30

    # Raw calendar fallback (None): ends at today
    s_raw, e_raw = resolve_date_filter("LAST_7_DAYS", t, has_conversion_metrics=None)
    assert e_raw == t
    assert s_raw == t - timedelta(days=6)


def test_confidence_calculation_math():
    """
    R3.3 (Q6): Confidence label calculation for rules with orders <= 0.
    Reference CVR = 0.05. p = (1 - 0.05)^clicks.
    clicks < 30 -> LOW_DATA ("Needs more data (< 30 clicks)").
    p < 0.05 (approx clicks >= 59) -> HIGH.
    Otherwise -> MEDIUM.
    """
    conds = [{"metric": "orders", "op": "LTE", "value": 0}]

    # 1. Reference CVR has < 30 clicks -> "Not enough data"
    conf_low = compute_confidence({"clicks": 25, "orders": 0}, conds, ad_group_ref=(20, 1))
    assert conf_low is not None
    assert conf_low["level"] == "LOW_DATA"
    assert conf_low["label"] == "Not enough data"

    # 2. Reference has 100 clicks, 5 orders -> ref_cvr = 0.05. Target clicks = 35 -> p = 0.95^35 ~= 0.166 -> Weak evidence
    conf_med = compute_confidence({"clicks": 35, "orders": 0}, conds, ad_group_ref=(100, 5))
    assert conf_med is not None
    assert conf_med["level"] == "MEDIUM"
    assert conf_med["label"] == "Weak evidence"
    assert conf_med["p_value"] > 0.05

    # 3. Target clicks = 60 -> p = 0.95^60 ~= 0.046 -> Strong evidence
    conf_high = compute_confidence({"clicks": 60, "orders": 0}, conds, ad_group_ref=(100, 5))
    assert conf_high is not None
    assert conf_high["level"] == "HIGH"
    assert conf_high["label"] == "Strong evidence"
    assert conf_high["p_value"] < 0.05

    # 4. Rule without orders <= 0 returns None
    conf_none = compute_confidence({"clicks": 60, "spend": 100}, [{"metric": "spend", "op": "GTE", "value": 50}])
    assert conf_none is None


@pytest.mark.asyncio
async def test_acos_vs_target_condition_and_skipping(db_session: AsyncSession, account_a: AdsAccount):
    """
    R3.2: Condition kind "acos_vs_target" compares ACOS against Target ACOS + offset.
    Campaigns without target ACOS are skipped and counted under skipped_no_target_acos.
    """
    today = today_for(account_a.marketplace)
    engine = get_engine()

    # Campaign 1: No target ACOS set
    c1 = Campaign(
        ads_account_id=account_a.id,
        name="Camp Without Target ACOS",
        state="ENABLED",
        targeting_type="MANUAL",
        campaign_type="SPONSORED_PRODUCTS",
        daily_budget=100.0,
        target_acos=None,
        start_date=today - timedelta(days=20),
    )
    # Campaign 2: Target ACOS = 25% set via CampaignBidThreshold
    c2 = Campaign(
        ads_account_id=account_a.id,
        name="Camp With Target ACOS",
        state="ENABLED",
        targeting_type="MANUAL",
        campaign_type="SPONSORED_PRODUCTS",
        daily_budget=200.0,
        target_acos=None,
        start_date=today - timedelta(days=20),
    )
    db_session.add_all([c1, c2])
    await db_session.flush()

    bt = CampaignBidThreshold(campaign_id=c2.id, target_acos=25.0)
    db_session.add(bt)
    await db_session.flush()

    # Add metrics for both campaigns (Spend 300, Sales 1000 => ACOS = 30.0%)
    m_date = today - timedelta(days=5)
    m1 = MetricDaily(
        date=m_date,
        entity_type="campaign",
        entity_id=c1.id,
        impressions=1000,
        clicks=50,
        spend=300.0,
        sales=1000.0,
        orders=10,
    )
    m2 = MetricDaily(
        date=m_date,
        entity_type="campaign",
        entity_id=c2.id,
        impressions=1000,
        clicks=50,
        spend=300.0,
        sales=1000.0,
        orders=10,
    )
    other_metrics = [
        MetricDaily(
            date=today - timedelta(days=d_offset),
            entity_type="campaign",
            entity_id=c.id,
            impressions=0,
            clicks=0,
            spend=0.0,
            sales=0.0,
            orders=0,
        )
        for c in (c1, c2)
        for d_offset in (2, 3, 4, 6, 7, 8)
    ]
    db_session.add_all([m1, m2] + other_metrics)
    await db_session.commit()

    # Rule: ACOS vs Target >= 2% (threshold = 25 + 2 = 27%). ACOS is 30% >= 27%
    rule = RecommendationRule(
        ads_account_id=account_a.id,
        name="High ACOS vs Target Rule",
        name_key="high acos vs target rule",
        affected_parameter="CAMPAIGN",
        date_filter="LAST_7_DAYS",
        min_clicks=10,
        conditions=[{"metric": "acos_vs_target", "op": "GTE", "value": 2.0}],
        actions=[{"type": "DECREASE_BUDGET", "params": {"pct": 10}}],
        is_active=True,
        scope_mode="ALL",
    )
    db_session.add(rule)
    await db_session.commit()
    await db_session.refresh(rule)

    candidates, funnels = await engine.evaluate(db_session, account_a, [rule], today=today, dry_run=True)
    funnel = funnels[rule.id]

    assert funnel["entities_in_scope"] == 2
    assert funnel["skipped_no_target_acos"] == 1  # c1 skipped
    assert len(candidates) == 1
    assert candidates[0].entity_id == c2.id
    assert candidates[0].matched[0]["target_acos"] == 25.0
    assert candidates[0].matched[0]["offset"] == 2.0


@pytest.mark.asyncio
async def test_campaign_status_daily_snapshot_condition(db_session: AsyncSession, account_a: AdsAccount):
    """
    R3.4 (Q8, F7): Condition 'out_of_budget_days >= 2' checks CampaignStatusDaily snapshots.
    Display-only rules with 0 actions are supported.
    """
    today = today_for(account_a.marketplace)
    engine = get_engine()

    camp = Campaign(
        ads_account_id=account_a.id,
        name="OOB Snapshot Test Camp",
        state="ENABLED",
        targeting_type="MANUAL",
        campaign_type="SPONSORED_PRODUCTS",
        daily_budget=100.0,
        start_date=today - timedelta(days=20),
    )
    db_session.add(camp)
    await db_session.flush()

    # Add 3 days of snapshots: 2 days OUT_OF_BUDGET, 1 day DELIVERING
    d1 = today - timedelta(days=4)
    d2 = today - timedelta(days=3)
    d3 = today - timedelta(days=2)

    s1 = CampaignStatusDaily(ads_account_id=account_a.id, campaign_id=camp.id, date=d1, state="ENABLED", serving_status="OUT_OF_BUDGET")
    s2 = CampaignStatusDaily(ads_account_id=account_a.id, campaign_id=camp.id, date=d2, state="ENABLED", serving_status="OUT_OF_BUDGET")
    s3 = CampaignStatusDaily(ads_account_id=account_a.id, campaign_id=camp.id, date=d3, state="ENABLED", serving_status="DELIVERING")
    db_session.add_all([s1, s2, s3])
    await db_session.commit()

    # Rule: CAMPAIGN rule with out_of_budget_days >= 2, 0 actions (display only)
    rule = RecommendationRule(
        ads_account_id=account_a.id,
        name="OOB Alert Rule",
        name_key="oob alert rule",
        affected_parameter="CAMPAIGN",
        date_filter="LAST_7_DAYS",
        min_clicks=0,
        conditions=[{"metric": "out_of_budget_days", "op": "GTE", "value": 2}],
        actions=[],  # Display only
        is_active=True,
        scope_mode="ALL",
    )
    db_session.add(rule)
    await db_session.commit()
    await db_session.refresh(rule)

    candidates, funnels = await engine.evaluate(db_session, account_a, [rule], today=today, dry_run=True)
    funnel = funnels[rule.id]

    assert len(candidates) == 1
    c = candidates[0]
    assert c.entity_id == camp.id
    assert c.metrics["out_of_budget_days"] == 2.0
    assert c.suggested_actions == []
    assert c.tracked_since == d1.isoformat()


@pytest.mark.asyncio
async def test_conflict_detection_and_apply_409(client_a: AsyncClient, db_session: AsyncSession, account_a: AdsAccount):
    """
    R3.5 (Q5): Conflict detection between recommendations on the same entity.
    API returns conflicts_with list.
    Bulk apply with conflicting items rejects with HTTP 409 naming both rules.
    """
    today = today_for(account_a.marketplace)

    camp = Campaign(
        ads_account_id=account_a.id,
        name="Conflict Test Camp",
        state="ENABLED",
        targeting_type="MANUAL",
        campaign_type="SPONSORED_PRODUCTS",
        daily_budget=100.0,
        start_date=today - timedelta(days=20),
    )
    db_session.add(camp)
    await db_session.flush()

    ag = AdGroup(campaign_id=camp.id, name="Conflict AG", default_bid=10.0)
    db_session.add(ag)
    await db_session.flush()

    kw = Keyword(ad_group_id=ag.id, keyword_text="conflict keyword", match_type="EXACT", bid=15.0, state="ENABLED")
    db_session.add(kw)
    await db_session.flush()

    # Rule 1: Decrease bid 10%
    r1 = Recommendation(
        ads_account_id=account_a.id,
        rule_id=None,
        rule_name="Rule 1 Lower Bid 10%",
        source="RULE",
        affected_parameter="TARGETING",
        entity_type="target",
        entity_id=kw.id,
        entity_label=kw.keyword_text,
        campaign_id=camp.id,
        ad_group_id=ag.id,
        evidence={"current": {"bid": 15.0, "state": "ENABLED"}},
        suggested_actions=[{"type": "DECREASE_BID", "params": {"pct": 10}, "from": 15.0, "to": 13.5}],
        status="OPEN",
        dedupe_key=f"rule1:target:{kw.id}",
    )
    # Rule 2: Decrease bid 20% on the same target (conflicts because both modify "bid")
    r2 = Recommendation(
        ads_account_id=account_a.id,
        rule_id=None,
        rule_name="Rule 2 Lower Bid 20%",
        source="RULE",
        affected_parameter="TARGETING",
        entity_type="target",
        entity_id=kw.id,
        entity_label=kw.keyword_text,
        campaign_id=camp.id,
        ad_group_id=ag.id,
        evidence={"current": {"bid": 15.0, "state": "ENABLED"}},
        suggested_actions=[{"type": "DECREASE_BID", "params": {"pct": 20}, "from": 15.0, "to": 12.0}],
        status="OPEN",
        dedupe_key=f"rule2:target:{kw.id}",
    )
    db_session.add_all([r1, r2])
    await db_session.commit()
    await db_session.refresh(r1)
    await db_session.refresh(r2)

    # 1. Unit test are_conflicting_recommendations
    assert are_conflicting_recommendations(r1, r2) is True
    assert are_conflicting_recommendations(r1, r1) is False

    # 2. GET /recommendations returns conflicts_with
    res = await client_a.get(f"/recommendations?ads_account_id={account_a.id}")
    assert res.status_code == 200
    items = res.json()["items"]
    r1_item = next(item for item in items if item["id"] == r1.id)
    r2_item = next(item for item in items if item["id"] == r2.id)
    assert r2.id in r1_item["conflicts_with"]
    assert r1.id in r2_item["conflicts_with"]

    # 3. POST /recommendations/bulk with action=APPLY and conflicting IDs -> HTTP 409
    bulk_payload = {
        "ids": [r1.id, r2.id],
        "action": "APPLY",
        "confirm": True,
        "idempotency_key": "conflict_test_1",
    }
    apply_res = await client_a.post("/recommendations/bulk", json=bulk_payload)
    assert apply_res.status_code == 409
    err_text = apply_res.json()["detail"]
    assert "Conflict" in err_text
    assert "Rule 1 Lower Bid 10%" in err_text
    assert "Rule 2 Lower Bid 20%" in err_text
