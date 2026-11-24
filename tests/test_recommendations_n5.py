import pytest
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from fastapi import HTTPException
from sqlalchemy import select

from app.config import settings
from app.models.ads_account import AdsAccount
from app.models.campaign import Campaign
from app.models.ad_group import AdGroup
from app.models.keyword import Keyword
from app.models.negative_keyword import NegativeKeyword
from app.models.metric_daily import MetricDaily
from app.models.recommendation_rule import RecommendationRule
from app.models.recommendation import Recommendation
from app.models.campaign_history import CampaignHistory
from app.recommendations.engine import run_engine
from app.recommendations.applier import apply_batch_recommendations
from app.recommendations.conflicts import check_rule_contradictions, check_rule_overlaps


@pytest.mark.asyncio
async def test_q12_freeze_periods_guard(db_session, user_a, account_a):
    """Q12: Freeze periods prevent recommendation generation and block Apply with 409."""
    camp = Campaign(
        ads_account_id=account_a.id,
        amazon_campaign_id="c-frz-1",
        name="Campaign Freeze",
        state="ENABLED",
        daily_budget=100.0,
        start_date=date(2026, 1, 1),
    )
    db_session.add(camp)
    await db_session.commit()

    rule = RecommendationRule(
        ads_account_id=account_a.id,
        name="Freeze Bid Rule",
        name_key="freeze bid rule",
        affected_parameter="TARGETING",
        date_filter="LAST_7_DAYS",
        min_clicks=5,
        conditions=[{"metric": "spend", "op": "GT", "value": 50}],
        actions=[{"type": "INCREASE_BID", "params": {"mode": "amount", "value": 2.0}}],
        is_active=True,
    )
    db_session.add(rule)
    await db_session.commit()

    from app.core.marketplace_rules import today_for
    today = today_for(account_a.marketplace)

    orig_freeze = settings.FREEZE_PERIODS
    settings.FREEZE_PERIODS = f"{(today - timedelta(days=2)).isoformat()}:{(today + timedelta(days=2)).isoformat()}"
    try:
        # 1. run_engine should generate 0 candidates during freeze
        res = await run_engine(db_session, account_a)
        assert res["created"] == 0
        assert "Freeze period active" in res.get("notice", "")

        # 2. Apply should raise 409 during freeze
        rec = Recommendation(
            ads_account_id=account_a.id,
            rule_id=rule.id,
            rule_name=rule.name,
            affected_parameter="TARGETING",
            entity_type="keyword",
            entity_id=999,
            entity_label="test keyword",
            campaign_id=camp.id,
            evidence={"metrics": {"spend": 100}},
            suggested_actions=[{"type": "INCREASE_BID", "from": 10.0, "to": 12.0}],
            status="OPEN",
            dedupe_key="freeze:test",
        )
        db_session.add(rec)
        await db_session.commit()

        with pytest.raises(HTTPException) as exc_info:
            await apply_batch_recommendations(db_session, user_a, [rec])
        assert exc_info.value.status_code == 409
        assert "freeze period is currently active" in exc_info.value.detail
    finally:
        settings.FREEZE_PERIODS = orig_freeze


@pytest.mark.asyncio
async def test_q13_search_term_safety_targeting_and_negative(db_session, user_a, account_a):
    """Q13: never Add as Targeting if already ENABLED kw or negated; never negate an ENABLED exact kw in same ad group."""
    today = date(2026, 11, 20)

    camp = Campaign(
        ads_account_id=account_a.id,
        amazon_campaign_id="c-st-1",
        name="Campaign ST 1",
        state="ENABLED",
        daily_budget=500.0,
        start_date=date(2026, 1, 1),
    )
    db_session.add(camp)
    await db_session.commit()

    ag = AdGroup(
        campaign_id=camp.id,
        name="AG ST 1",
        default_bid=15.0,
        state="ENABLED",
    )
    db_session.add(ag)
    await db_session.commit()

    # Existing active EXACT keyword "running shoes" in ag
    kw_exact = Keyword(
        ad_group_id=ag.id,
        keyword_text="running shoes",
        match_type="EXACT",
        state="ENABLED",
        bid=15.0,
    )
    db_session.add(kw_exact)

    # Existing negative keyword "bad shoes" in camp
    neg_kw = NegativeKeyword(
        campaign_id=camp.id,
        keyword_text="bad shoes",
        match_type="NEGATIVE_EXACT",
    )
    db_session.add(neg_kw)
    await db_session.commit()

    from app.models.search_term import SearchTerm
    st_exact = SearchTerm(
        campaign_id=camp.id,
        ad_group_id=ag.id,
        keyword_id=kw_exact.id,
        search_term="running shoes",
        date=today - timedelta(days=3),
        impressions=100,
        clicks=20,
        spend=200.0,
        sales=0.0,
        orders=0,
    )
    st_negated = SearchTerm(
        campaign_id=camp.id,
        ad_group_id=ag.id,
        keyword_id=kw_exact.id,
        search_term="bad shoes",
        date=today - timedelta(days=3),
        impressions=80,
        clicks=15,
        spend=150.0,
        sales=0.0,
        orders=0,
    )
    db_session.add_all([st_exact, st_negated])
    await db_session.commit()

    from app.recommendations.engine import get_engine
    engine = get_engine()

    # Rule 1: Negate search terms with orders <= 0
    rule_neg = RecommendationRule(
        ads_account_id=account_a.id,
        name="Negate Poor Search Terms",
        name_key="negate poor search terms",
        affected_parameter="SEARCH_TERM",
        date_filter="LAST_7_DAYS",
        min_clicks=5,
        conditions=[{"metric": "orders", "op": "LTE", "value": 0}],
        actions=[{"type": "ADD_AS_NEGATIVE", "params": {"match_type": "NEGATIVE_EXACT", "level": "AD_GROUP"}}],
        is_active=True,
    )

    candidates, funnels = await engine.evaluate(db_session, account_a, [rule_neg], today=today, dry_run=True)
    # "running shoes" must NOT generate a negative action because it exists as an enabled exact keyword in this ad group!
    # Instead, it must be flagged with CANNOT_NEGATE_ACTIVE_EXACT_KEYWORD
    for c in candidates:
        if c.entity_label == "running shoes":
            assert c.safety_flag == "CANNOT_NEGATE_ACTIVE_EXACT_KEYWORD"
            act_types = [a["type"] for a in c.suggested_actions]
            assert "ADD_AS_NEGATIVE" not in act_types

    # Rule 2: Add search terms as targeting
    rule_add = RecommendationRule(
        ads_account_id=account_a.id,
        name="Add Good Search Terms",
        name_key="add good search terms",
        affected_parameter="SEARCH_TERM",
        date_filter="LAST_7_DAYS",
        min_clicks=5,
        conditions=[{"metric": "clicks", "op": "GTE", "value": 5}],
        actions=[{"type": "ADD_AS_TARGETING", "params": {"match_type": "EXACT", "bid": 20.0}}],
        is_active=True,
    )
    candidates_add, _ = await engine.evaluate(db_session, account_a, [rule_add], today=today, dry_run=True)
    # Neither "running shoes" (already enabled exact kw) nor "bad shoes" (already negated) should be added as targeting!
    for c in candidates_add:
        assert c.entity_label != "running shoes"
        assert c.entity_label != "bad shoes"


@pytest.mark.asyncio
async def test_q14_rule_contradiction_and_overlap_warnings(client_a, account_a):
    """Q14: Server returns contradiction and overlap warnings in /recommendations/rules/preview."""
    rule_draft = {
        "name": "Contradictory Rule",
        "affected_parameter": "TARGETING",
        "date_filter": "LAST_30_DAYS",
        "min_clicks": 10,
        "conditions": [
            {"metric": "orders", "op": "GTE", "value": 5},
            {"metric": "orders", "op": "LTE", "value": 0},
        ],
        "actions": [{"type": "INCREASE_BID", "params": {"mode": "percent", "value": 15}}],
        "scope_mode": "ALL",
        "is_active": True,
    }

    resp = await client_a.post(
        "/recommendations/rules/preview",
        json={"ads_account_id": account_a.id, "rule": rule_draft},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "warnings" in data
    assert any("Contradictory conditions on 'orders'" in w for w in data["warnings"])


@pytest.mark.asyncio
async def test_q17_clone_and_run_now_rules(client_a, account_a):
    """Q17: Clone rule creates inactive copy; run-now runs evaluation for that specific rule."""
    rule_draft = {
        "name": "Rule to Clone",
        "affected_parameter": "CAMPAIGN",
        "date_filter": "LAST_7_DAYS",
        "min_clicks": 1,
        "conditions": [{"metric": "spend", "op": "GT", "value": 10}],
        "actions": [{"type": "SET_BUDGET", "params": {"value": 500.0}}],
        "scope_mode": "ALL",
        "is_active": True,
    }
    create_resp = await client_a.post(
        f"/recommendations/rules?ads_account_id={account_a.id}",
        json=rule_draft,
    )
    assert create_resp.status_code == 200
    orig_id = create_resp.json()["rule"]["id"]

    # 1. Clone rule
    clone_resp = await client_a.post(
        f"/recommendations/rules/{orig_id}/clone",
    )
    assert clone_resp.status_code == 200
    clone_data = clone_resp.json()
    assert clone_data["name"] == "Rule to Clone (copy)"
    assert clone_data["is_active"] is False

    # 2. Run now
    run_resp = await client_a.post(
        f"/recommendations/rules/{orig_id}/run-now",
    )
    assert run_resp.status_code == 200
    run_data = run_resp.json()
    assert run_data["rule_id"] == orig_id
    assert "created" in run_data
    assert "funnel" in run_data


@pytest.mark.asyncio
async def test_q17_snooze_and_undo_recommendation(db_session, client_a, account_a):
    """Q17: Snooze hides recommendation for 7/14/30d; Undo verifies live value and reverts change."""
    camp = Campaign(
        ads_account_id=account_a.id,
        amazon_campaign_id="c-undo-1",
        name="Campaign Undo",
        state="ENABLED",
        daily_budget=200.0,
        start_date=date(2026, 1, 1),
    )
    db_session.add(camp)
    await db_session.commit()

    ag = AdGroup(campaign_id=camp.id, name="AG Undo", default_bid=10.0, state="ENABLED")
    db_session.add(ag)
    await db_session.commit()

    kw = Keyword(ad_group_id=ag.id, keyword_text="test kw", match_type="EXACT", state="ENABLED", bid=10.0)
    db_session.add(kw)
    await db_session.commit()

    # 1. Test Snooze
    rec_snooze = Recommendation(
        ads_account_id=account_a.id,
        rule_name="Snooze Rule",
        affected_parameter="TARGETING",
        entity_type="keyword",
        entity_id=kw.id,
        entity_label="test kw",
        campaign_id=camp.id,
        ad_group_id=ag.id,
        evidence={"metrics": {"spend": 50}},
        suggested_actions=[{"type": "INCREASE_BID", "from": 10.0, "to": 12.0}],
        status="OPEN",
        dedupe_key="snooze:kw:1",
    )
    db_session.add(rec_snooze)
    await db_session.commit()

    snooze_resp = await client_a.post(
        f"/recommendations/{rec_snooze.id}/snooze",
        json={"days": 14},
    )
    assert snooze_resp.status_code == 200
    assert snooze_resp.json()["display_status"] == "SNOOZED"
    assert snooze_resp.json()["status"] == "DISMISSED"

    # 2. Test Undo
    rec_undo = Recommendation(
        ads_account_id=account_a.id,
        rule_name="Undo Rule",
        affected_parameter="TARGETING",
        entity_type="keyword",
        entity_id=kw.id,
        entity_label="test kw",
        campaign_id=camp.id,
        ad_group_id=ag.id,
        evidence={"metrics": {"spend": 100}},
        suggested_actions=[{"type": "INCREASE_BID", "from": 10.0, "to": 12.0}],
        status="APPLIED",
        resolved_at=datetime.now(timezone.utc) - timedelta(days=2),
        apply_result={
            "changes": [{"field": "bid", "from": 10.0, "to": 12.0}],
            "notes": ["Applied bid increase"],
        },
        dedupe_key="undo:kw:1",
    )
    kw.bid = 12.0
    db_session.add(rec_undo)
    await db_session.commit()

    undo_resp = await client_a.post(
        f"/recommendations/{rec_undo.id}/undo",
    )
    assert undo_resp.status_code == 200
    undo_data = undo_resp.json()
    assert undo_data["display_status"] == "UNDONE"
    await db_session.refresh(kw)
    assert abs(Decimal(str(kw.bid)) - Decimal("10.0")) < Decimal("0.01")

    # Verify CampaignHistory recorded the undo
    ch = (
        await db_session.execute(
            select(CampaignHistory).where(
                CampaignHistory.recommendation_id == rec_undo.id,
                CampaignHistory.event_type == "RECOMMENDATION_UNDONE",
            )
        )
    ).scalars().first()
    assert ch is not None
    assert ch.previous_value == "12.0"
    assert ch.new_value == "10.0"


@pytest.mark.asyncio
async def test_q19_outcome_tracking(db_session, client_a, account_a):
    """Q19: Outcome tracking computes 7 days before vs 7 days after for applied items."""
    applied_time = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)

    camp = Campaign(
        ads_account_id=account_a.id,
        amazon_campaign_id="c-outcome-1",
        name="Campaign Outcome",
        state="ENABLED",
        daily_budget=200.0,
        start_date=date(2026, 1, 1),
    )
    db_session.add(camp)
    await db_session.commit()

    ag = AdGroup(campaign_id=camp.id, name="AG Outcome", default_bid=10.0, state="ENABLED")
    db_session.add(ag)
    await db_session.commit()

    kw = Keyword(ad_group_id=ag.id, keyword_text="outcome kw", match_type="EXACT", state="ENABLED", bid=15.0)
    db_session.add(kw)
    await db_session.commit()

    # Seed 7 days before metrics (2026-11-03 to 2026-11-09)
    for i in range(3, 10):
        db_session.add(
            MetricDaily(
                entity_type="keyword",
                entity_id=kw.id,
                date=date(2026, 11, i),
                impressions=100,
                clicks=10,
                spend=50.0,
                sales=200.0,
                orders=2,
            )
        )

    # Seed 7 days after metrics (2026-11-11 to 2026-11-17)
    for i in range(11, 18):
        db_session.add(
            MetricDaily(
                entity_type="keyword",
                entity_id=kw.id,
                date=date(2026, 11, i),
                impressions=120,
                clicks=12,
                spend=60.0,
                sales=300.0,
                orders=3,
            )
        )
    await db_session.commit()

    rec = Recommendation(
        ads_account_id=account_a.id,
        rule_name="Outcome Rule",
        affected_parameter="TARGETING",
        entity_type="keyword",
        entity_id=kw.id,
        entity_label="outcome kw",
        campaign_id=camp.id,
        ad_group_id=ag.id,
        evidence={"metrics": {"spend": 350.0}},
        suggested_actions=[{"type": "INCREASE_BID", "from": 10.0, "to": 15.0}],
        status="APPLIED",
        resolved_at=applied_time,
        apply_result={"changes": [{"field": "bid", "from": 10.0, "to": 15.0}]},
        dedupe_key="outcome:kw:1",
    )
    db_session.add(rec)
    await db_session.commit()

    resp = await client_a.get(f"/recommendations/{rec.id}/outcome")
    assert resp.status_code == 200
    data = resp.json()
    assert data["eligible"] is True
    assert data["disclaimer"] == "Before/after comparison, not proof of cause"
    assert data["before"]["spend"] == 350.0
    assert data["before"]["orders"] == 14
    assert data["after"]["spend"] == 420.0
    assert data["after"]["orders"] == 21
    assert data["delta"]["spend_diff"] == 70.0
    assert data["delta"]["orders_diff"] == 7
