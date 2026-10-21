from datetime import date, datetime, timedelta, timezone
import pytest
from httpx import AsyncClient
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.marketplace_rules import today_for
from app.models.ads_account import AdsAccount
from app.models.campaign import Campaign
from app.models.ad_group import AdGroup
from app.models.keyword import Keyword
from app.models.metric_daily import MetricDaily
from app.models.negative_keyword import NegativeKeyword
from app.models.recommendation import Recommendation
from app.models.recommendation_rule import RecommendationRule, RecommendationRuleCampaign
from app.models.recommendation_run import RecommendationRun
from app.models.search_term import SearchTerm
from app.models.user import User
from app.providers.ads.mock_provider import MockAdsProvider
from app.recommendations.engine import RuleBasedEngine, get_engine, run_engine
from app.recommendations.registry import TEMPLATES, resolve_template, resolve_value


@pytest.mark.asyncio
async def test_each_template_produces_expected_candidates_on_fresh_account(
    db_session: AsyncSession, account_a: AdsAccount
):
    """Verify each non-placement template produces >= 1 candidate on a fresh mock account."""
    provider = MockAdsProvider(db_session)
    await provider.generate_for_new_connection(account_a.id)

    engine = get_engine()
    today = today_for(account_a.marketplace)

    # All 7 non-placement templates
    non_placement_templates = [t for t in TEMPLATES if t.get("affected_parameter") != "PLACEMENT"]
    assert len(non_placement_templates) == 7

    for t in non_placement_templates:
        resolved = resolve_template(t, account_a.marketplace)
        rule = RecommendationRule(
            id=100,
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
        assert len(candidates) >= 1, f"Template {t['key']} produced 0 candidates on fresh mock data"

        # Check candidate structure
        for c in candidates:
            assert c.entity_type in ("campaign", "target", "search_term")
            assert c.entity_label
            assert c.campaign_id
            assert isinstance(c.metrics, dict)
            assert len(c.matched) >= 1
            assert len(c.suggested_actions) >= 1
            assert c.dedupe_key.startswith("100:")


@pytest.mark.asyncio
async def test_acos_infinity_rule_semantics(db_session: AsyncSession, account_a: AdsAccount):
    """
    ACOS +inf semantics:
    - spend with no sales matches acos >= 50
    - spend with no sales does not match acos <= 15
    - spend == 0 never matches acos conditions
    """
    engine = get_engine()
    today = today_for(account_a.marketplace)

    camp = Campaign(
        ads_account_id=account_a.id,
        name="Campaign ACOS Test",
        state="ENABLED",
        targeting_type="MANUAL",
        campaign_type="SPONSORED_PRODUCTS",
        daily_budget=50.0,
        start_date=today - timedelta(days=10),
    )
    db_session.add(camp)
    await db_session.flush()

    ag = AdGroup(campaign_id=camp.id, name="AG ACOS", default_bid=1.0)
    db_session.add(ag)
    await db_session.flush()

    kw_bleeding = Keyword(ad_group_id=ag.id, keyword_text="bleeding keyword", match_type="EXACT", bid=1.0, state="ENABLED")
    kw_zero_spend = Keyword(ad_group_id=ag.id, keyword_text="zero spend keyword", match_type="EXACT", bid=1.0, state="ENABLED")
    db_session.add_all([kw_bleeding, kw_zero_spend])
    await db_session.flush()

    # Bleeding keyword: spend = 30.0, sales = 0.0 (no sales), orders = 0
    m_bleeding = MetricDaily(
        entity_type="keyword",
        entity_id=kw_bleeding.id,
        date=today - timedelta(days=2),
        impressions=100,
        clicks=25,
        spend=30.0,
        sales=0.0,
        orders=0,
    )
    # Zero spend keyword: impressions = 10, clicks = 0, spend = 0, sales = 0
    m_zero = MetricDaily(
        entity_type="keyword",
        entity_id=kw_zero_spend.id,
        date=today - timedelta(days=2),
        impressions=10,
        clicks=0,
        spend=0.0,
        sales=0.0,
        orders=0,
    )
    db_session.add_all([m_bleeding, m_zero])
    await db_session.commit()

    # Rule 1: ACOS >= 50
    rule_gte = RecommendationRule(
        id=1,
        ads_account_id=account_a.id,
        name="High ACOS Rule",
        affected_parameter="TARGETING",
        date_filter="LAST_7_DAYS",
        min_clicks=0,
        conditions=[{"metric": "acos", "op": "GTE", "value": 50.0}],
        actions=[{"type": "DECREASE_BID", "params": {"pct": 20}}],
        scope_mode="ALL",
        is_active=True,
    )
    res_gte = await engine.evaluate(db_session, account_a, [rule_gte], today=today, dry_run=True)
    matched_ids_gte = [c.entity_id for c in res_gte]
    assert kw_bleeding.id in matched_ids_gte, "spend with no sales MUST match acos >= 50"
    assert kw_zero_spend.id not in matched_ids_gte, "spend == 0 must NOT match acos >= 50"

    # Rule 2: ACOS <= 15
    rule_lte = RecommendationRule(
        id=2,
        ads_account_id=account_a.id,
        name="Low ACOS Rule",
        affected_parameter="TARGETING",
        date_filter="LAST_7_DAYS",
        min_clicks=0,
        conditions=[{"metric": "acos", "op": "LTE", "value": 15.0}],
        actions=[{"type": "INCREASE_BID", "params": {"pct": 15}}],
        scope_mode="ALL",
        is_active=True,
    )
    res_lte = await engine.evaluate(db_session, account_a, [rule_lte], today=today, dry_run=True)
    matched_ids_lte = [c.entity_id for c in res_lte]
    assert kw_bleeding.id not in matched_ids_lte, "spend with no sales MUST NOT match acos <= 15"
    assert kw_zero_spend.id not in matched_ids_lte, "spend == 0 must NOT match acos <= 15"


@pytest.mark.asyncio
async def test_min_clicks_skips_small_samples(db_session: AsyncSession, account_a: AdsAccount):
    """min_clicks safeguard skips entities with fewer clicks than rule threshold."""
    engine = get_engine()
    today = today_for(account_a.marketplace)

    camp = Campaign(
        ads_account_id=account_a.id,
        name="Campaign Min Clicks",
        state="ENABLED",
        targeting_type="MANUAL",
        campaign_type="SPONSORED_PRODUCTS",
        daily_budget=50.0,
        start_date=today - timedelta(days=10),
    )
    db_session.add(camp)
    await db_session.flush()

    ag = AdGroup(campaign_id=camp.id, name="AG Min Clicks", default_bid=1.0)
    db_session.add(ag)
    await db_session.flush()

    kw = Keyword(ad_group_id=ag.id, keyword_text="few clicks kw", match_type="EXACT", bid=1.0, state="ENABLED")
    db_session.add(kw)
    await db_session.flush()

    # 5 clicks only
    m = MetricDaily(
        entity_type="keyword",
        entity_id=kw.id,
        date=today - timedelta(days=2),
        impressions=50,
        clicks=5,
        spend=10.0,
        sales=0.0,
        orders=0,
    )
    db_session.add(m)
    await db_session.commit()

    # Rule with min_clicks=10 should skip this entity
    rule_strict = RecommendationRule(
        id=10,
        ads_account_id=account_a.id,
        name="Strict Clicks",
        affected_parameter="TARGETING",
        date_filter="LAST_7_DAYS",
        min_clicks=10,
        conditions=[{"metric": "spend", "op": "GTE", "value": 5.0}],
        actions=[{"type": "PAUSE_TARGET", "params": {}}],
        scope_mode="ALL",
        is_active=True,
    )
    res_strict = await engine.evaluate(db_session, account_a, [rule_strict], today=today, dry_run=True)
    assert len(res_strict) == 0

    # Rule with min_clicks=5 should match
    rule_lax = RecommendationRule(
        id=11,
        ads_account_id=account_a.id,
        name="Lax Clicks",
        affected_parameter="TARGETING",
        date_filter="LAST_7_DAYS",
        min_clicks=5,
        conditions=[{"metric": "spend", "op": "GTE", "value": 5.0}],
        actions=[{"type": "PAUSE_TARGET", "params": {}}],
        scope_mode="ALL",
        is_active=True,
    )
    res_lax = await engine.evaluate(db_session, account_a, [rule_lax], today=today, dry_run=True)
    assert len(res_lax) == 1
    assert res_lax[0].entity_id == kw.id


@pytest.mark.asyncio
async def test_scope_include_exclude_and_paused_skipped(db_session: AsyncSession, account_a: AdsAccount):
    """Scope filtering: INCLUDE, EXCLUDE, and paused campaigns are skipped."""
    engine = get_engine()
    today = today_for(account_a.marketplace)

    c1 = Campaign(ads_account_id=account_a.id, name="Camp 1", state="ENABLED", daily_budget=50.0, start_date=today - timedelta(days=5))
    c2 = Campaign(ads_account_id=account_a.id, name="Camp 2", state="ENABLED", daily_budget=50.0, start_date=today - timedelta(days=5))
    c_paused = Campaign(ads_account_id=account_a.id, name="Camp Paused", state="PAUSED", daily_budget=50.0, start_date=today - timedelta(days=5))
    db_session.add_all([c1, c2, c_paused])
    await db_session.flush()

    # Add daily metrics for all three
    for c in (c1, c2, c_paused):
        m = MetricDaily(
            entity_type="campaign",
            entity_id=c.id,
            date=today - timedelta(days=1),
            impressions=100,
            clicks=10,
            spend=40.0,
            sales=200.0,
            orders=5,
        )
        db_session.add(m)
    await db_session.flush()

    # 1. Scope INCLUDE c1 only
    rule_inc = RecommendationRule(
        id=20,
        ads_account_id=account_a.id,
        name="Include Rule",
        name_key="include rule",
        affected_parameter="CAMPAIGN",
        date_filter="LAST_7_DAYS",
        min_clicks=0,
        conditions=[{"metric": "spend", "op": "GTE", "value": 10.0}],
        actions=[{"type": "INCREASE_BUDGET", "params": {"pct": 10}}],
        scope_mode="INCLUDE",
        is_active=True,
    )
    db_session.add(rule_inc)
    await db_session.flush()
    db_session.add(RecommendationRuleCampaign(rule_id=rule_inc.id, campaign_id=c1.id))
    await db_session.commit()

    res_inc = await engine.evaluate(db_session, account_a, [rule_inc], today=today, dry_run=True)
    assert len(res_inc) == 1
    assert res_inc[0].campaign_id == c1.id

    # 2. Scope EXCLUDE c1 (should match c2 only, since c_paused is paused)
    rule_exc = RecommendationRule(
        id=21,
        ads_account_id=account_a.id,
        name="Exclude Rule",
        name_key="exclude rule",
        affected_parameter="CAMPAIGN",
        date_filter="LAST_7_DAYS",
        min_clicks=0,
        conditions=[{"metric": "spend", "op": "GTE", "value": 10.0}],
        actions=[{"type": "INCREASE_BUDGET", "params": {"pct": 10}}],
        scope_mode="EXCLUDE",
        is_active=True,
    )
    db_session.add(rule_exc)
    await db_session.flush()
    db_session.add(RecommendationRuleCampaign(rule_id=rule_exc.id, campaign_id=c1.id))
    await db_session.commit()
    await db_session.commit()

    res_exc = await engine.evaluate(db_session, account_a, [rule_exc], today=today, dry_run=True)
    assert len(res_exc) == 1
    assert res_exc[0].campaign_id == c2.id


@pytest.mark.asyncio
async def test_running_twice_creates_no_duplicates(db_session: AsyncSession, account_a: AdsAccount):
    """Running the engine twice creates no duplicate recommendations."""
    provider = MockAdsProvider(db_session)
    await provider.generate_for_new_connection(account_a.id)

    rule = RecommendationRule(
        ads_account_id=account_a.id,
        name="Negate Wasted Terms Live",
        name_key="negate wasted terms live",
        affected_parameter="SEARCH_TERM",
        date_filter="LAST_30_DAYS",
        min_clicks=0,
        conditions=[{"metric": "spend", "op": "GTE", "value": 20.0}, {"metric": "orders", "op": "LTE", "value": 0}],
        actions=[{"type": "ADD_AS_NEGATIVE", "params": {"match_type": "NEGATIVE_EXACT", "level": "AD_GROUP"}}],
        scope_mode="ALL",
        is_active=True,
    )
    db_session.add(rule)
    await db_session.commit()
    await db_session.refresh(rule)

    # First run
    run1 = await run_engine(db_session, account_a, rule_ids=[rule.id], trigger="manual")
    created1 = run1["created"]
    assert created1 > 0
    assert run1["skipped"] == 0

    recs_count_1 = (await db_session.execute(
        select(func.count(Recommendation.id)).where(Recommendation.rule_id == rule.id, Recommendation.status == "OPEN")
    )).scalar()
    assert recs_count_1 == created1

    # Second run
    run2 = await run_engine(db_session, account_a, rule_ids=[rule.id], trigger="manual")
    assert run2["created"] == 0
    assert run2["skipped"] == created1

    recs_count_2 = (await db_session.execute(
        select(func.count(Recommendation.id)).where(Recommendation.rule_id == rule.id, Recommendation.status == "OPEN")
    )).scalar()
    assert recs_count_2 == recs_count_1


@pytest.mark.asyncio
async def test_expired_when_situation_no_longer_matches(db_session: AsyncSession, account_a: AdsAccount):
    """When conditions or data change, previous OPEN recommendations become EXPIRED."""
    today = today_for(account_a.marketplace)

    camp = Campaign(ads_account_id=account_a.id, name="Camp Expire", state="ENABLED", daily_budget=50.0, start_date=today - timedelta(days=5))
    db_session.add(camp)
    await db_session.flush()

    m = MetricDaily(
        entity_type="campaign",
        entity_id=camp.id,
        date=today - timedelta(days=1),
        impressions=100,
        clicks=10,
        spend=40.0,
        sales=200.0,
        orders=5,
    )
    db_session.add(m)

    rule = RecommendationRule(
        ads_account_id=account_a.id,
        name="Expire Rule Test",
        name_key="expire rule test",
        affected_parameter="CAMPAIGN",
        date_filter="LAST_7_DAYS",
        min_clicks=0,
        conditions=[{"metric": "spend", "op": "GTE", "value": 30.0}],
        actions=[{"type": "INCREASE_BUDGET", "params": {"pct": 10}}],
        scope_mode="ALL",
        is_active=True,
    )
    db_session.add(rule)
    await db_session.commit()
    await db_session.refresh(rule)

    # 1. Run engine -> creates 1 OPEN recommendation
    run1 = await run_engine(db_session, account_a, rule_ids=[rule.id], trigger="manual")
    assert run1["created"] == 1

    rec = (await db_session.execute(
        select(Recommendation).where(Recommendation.rule_id == rule.id)
    )).scalars().first()
    assert rec.status == "OPEN"

    # 2. Modify rule condition so the entity no longer qualifies (spend >= 1000)
    rule.conditions = [{"metric": "spend", "op": "GTE", "value": 1000.0}]
    await db_session.commit()

    # 3. Re-run engine -> previous OPEN recommendation becomes EXPIRED
    run2 = await run_engine(db_session, account_a, rule_ids=[rule.id], trigger="manual")
    assert run2["created"] == 0
    assert run2["expired"] == 1

    await db_session.refresh(rec)
    assert rec.status == "EXPIRED"
    assert rec.resolved_at is not None


@pytest.mark.asyncio
async def test_cooldown_after_dismiss(client_a: AsyncClient, db_session: AsyncSession, account_a: AdsAccount):
    """A dismissed recommendation is not re-created while in cooldown_days."""
    today = today_for(account_a.marketplace)

    camp = Campaign(ads_account_id=account_a.id, name="Camp Cooldown", state="ENABLED", daily_budget=50.0, start_date=today - timedelta(days=5))
    db_session.add(camp)
    await db_session.flush()

    m = MetricDaily(
        entity_type="campaign",
        entity_id=camp.id,
        date=today - timedelta(days=1),
        impressions=100,
        clicks=10,
        spend=40.0,
        sales=200.0,
        orders=5,
    )
    db_session.add(m)

    rule = RecommendationRule(
        ads_account_id=account_a.id,
        name="Cooldown Test Rule",
        name_key="cooldown test rule",
        affected_parameter="CAMPAIGN",
        date_filter="LAST_7_DAYS",
        min_clicks=0,
        conditions=[{"metric": "spend", "op": "GTE", "value": 20.0}],
        actions=[{"type": "INCREASE_BUDGET", "params": {"pct": 10}}],
        scope_mode="ALL",
        is_active=True,
    )
    db_session.add(rule)
    await db_session.commit()

    # Run engine -> 1 OPEN rec
    await run_engine(db_session, account_a, rule_ids=[rule.id], trigger="manual")
    rec = (await db_session.execute(
        select(Recommendation).where(Recommendation.rule_id == rule.id, Recommendation.status == "OPEN")
    )).scalars().first()
    assert rec is not None

    # Dismiss rec via API
    r_dismiss = await client_a.post(f"/recommendations/{rec.id}/dismiss", json={"reason": "Not relevant"})
    assert r_dismiss.status_code == 200
    # Refresh rec in test session
    await db_session.refresh(rec)
    assert rec.status == "DISMISSED"

    # Re-run engine within cooldown period -> should skip candidate and NOT recreate OPEN
    run2 = await run_engine(db_session, account_a, rule_ids=[rule.id], trigger="manual")
    assert run2["created"] == 0
    assert run2["skipped"] == 1

    # Verify still only 1 rec in DB with DISMISSED status
    statuses = (await db_session.execute(
        select(Recommendation.status).where(Recommendation.rule_id == rule.id)
    )).scalars().all()
    assert len(statuses) == 1
    assert statuses[0] == "DISMISSED"


@pytest.mark.asyncio
async def test_apply_add_as_negative_idempotent_and_conflict(client_a: AsyncClient, db_session: AsyncSession, account_a: AdsAccount):
    """
    Applying ADD_AS_NEGATIVE:
    - creates NegativeKeyword
    - second apply is idempotent
    - exact negative equal to enabled positive EXACT keyword fails with 422
    """
    today = today_for(account_a.marketplace)

    camp = Campaign(ads_account_id=account_a.id, name="Camp Neg Test", state="ENABLED", daily_budget=50.0, start_date=today - timedelta(days=5))
    db_session.add(camp)
    await db_session.flush()

    ag = AdGroup(campaign_id=camp.id, name="AG Neg", default_bid=1.0)
    db_session.add(ag)
    await db_session.flush()

    rec = Recommendation(
        ads_account_id=account_a.id,
        rule_name="Negate Rule",
        affected_parameter="SEARCH_TERM",
        entity_type="search_term",
        entity_label="wasteful term",
        campaign_id=camp.id,
        ad_group_id=ag.id,
        evidence={"metrics": {"spend": 25.0, "orders": 0}, "matched": [], "current": {}},
        suggested_actions=[{"type": "ADD_AS_NEGATIVE", "params": {"match_type": "NEGATIVE_EXACT", "level": "AD_GROUP"}}],
        status="OPEN",
        dedupe_key="neg:1",
    )
    db_session.add(rec)
    await db_session.commit()

    # 1. Apply recommendation
    r_apply = await client_a.post(f"/recommendations/{rec.id}/apply")
    assert r_apply.status_code == 200
    data = r_apply.json()
    assert data["status"] == "APPLIED"
    assert data["apply_result"]["changes"][0]["field"] == "negative_keyword"

    # Exactly 1 NegativeKeyword created in DB
    neg_kw = (await db_session.execute(
        select(NegativeKeyword).where(NegativeKeyword.campaign_id == camp.id, NegativeKeyword.keyword_text == "wasteful term")
    )).scalars().all()
    assert len(neg_kw) == 1

    # 2. Idempotency test: apply another recommendation with same negative keyword
    rec2 = Recommendation(
        ads_account_id=account_a.id,
        rule_name="Negate Rule 2",
        affected_parameter="SEARCH_TERM",
        entity_type="search_term",
        entity_label="wasteful term",
        campaign_id=camp.id,
        ad_group_id=ag.id,
        evidence={"metrics": {"spend": 30.0, "orders": 0}, "matched": [], "current": {}},
        suggested_actions=[{"type": "ADD_AS_NEGATIVE", "params": {"match_type": "NEGATIVE_EXACT", "level": "AD_GROUP"}}],
        status="OPEN",
        dedupe_key="neg:2",
    )
    db_session.add(rec2)
    await db_session.commit()

    r_apply2 = await client_a.post(f"/recommendations/{rec2.id}/apply")
    assert r_apply2.status_code == 200
    assert "already exists" in r_apply2.json()["apply_result"]["notes"][0]

    # Still exactly 1 NegativeKeyword
    neg_kw_after = (await db_session.execute(
        select(NegativeKeyword).where(NegativeKeyword.campaign_id == camp.id, NegativeKeyword.keyword_text == "wasteful term")
    )).scalars().all()
    assert len(neg_kw_after) == 1

    # 3. Conflict test: enabled positive EXACT keyword in campaign
    pos_kw = Keyword(ad_group_id=ag.id, keyword_text="conflicting term", match_type="EXACT", bid=1.0, state="ENABLED")
    db_session.add(pos_kw)

    rec_conflict = Recommendation(
        ads_account_id=account_a.id,
        rule_name="Conflict Neg Rule",
        affected_parameter="SEARCH_TERM",
        entity_type="search_term",
        entity_label="conflicting term",
        campaign_id=camp.id,
        ad_group_id=ag.id,
        evidence={"metrics": {"spend": 10.0, "orders": 0}, "matched": [], "current": {}},
        suggested_actions=[{"type": "ADD_AS_NEGATIVE", "params": {"match_type": "NEGATIVE_EXACT", "level": "AD_GROUP"}}],
        status="OPEN",
        dedupe_key="neg:conflict",
    )
    db_session.add(rec_conflict)
    await db_session.commit()

    r_conflict = await client_a.post(f"/recommendations/{rec_conflict.id}/apply")
    assert r_conflict.status_code == 422
    assert "conflicts with an enabled positive EXACT keyword" in str(r_conflict.json())


@pytest.mark.asyncio
async def test_apply_with_changed_bid_returns_409(client_a: AsyncClient, db_session: AsyncSession, account_a: AdsAccount):
    """Stale check: if live bid changed since analysis, apply returns 409."""
    today = today_for(account_a.marketplace)

    camp = Campaign(ads_account_id=account_a.id, name="Camp Stale", state="ENABLED", daily_budget=50.0, start_date=today - timedelta(days=5))
    db_session.add(camp)
    await db_session.flush()

    ag = AdGroup(campaign_id=camp.id, name="AG Stale", default_bid=1.0)
    db_session.add(ag)
    await db_session.flush()

    kw = Keyword(ad_group_id=ag.id, keyword_text="stale kw", match_type="EXACT", bid=1.0, state="ENABLED")
    db_session.add(kw)
    await db_session.flush()

    # Recommendation was analyzed when kw.bid was 1.0
    rec = Recommendation(
        ads_account_id=account_a.id,
        rule_name="Bid Rule",
        affected_parameter="TARGETING",
        entity_type="target",
        entity_id=kw.id,
        entity_label="stale kw",
        campaign_id=camp.id,
        ad_group_id=ag.id,
        evidence={"metrics": {"clicks": 20, "orders": 5}, "matched": [], "current": {"bid": 1.0, "state": "ENABLED"}},
        suggested_actions=[{"type": "INCREASE_BID", "params": {"pct": 20}, "from": 1.0, "to": 1.2}],
        status="OPEN",
        dedupe_key="stale:1",
    )
    db_session.add(rec)
    await db_session.commit()

    # Now change keyword bid to 1.50
    kw.bid = 1.50
    await db_session.commit()

    # Apply -> 409 Conflict
    r = await client_a.post(f"/recommendations/{rec.id}/apply")
    assert r.status_code == 409
    assert "This value changed since the analysis — run the analysis again" in r.json()["detail"]

    # Recommendation remains OPEN
    await db_session.refresh(rec)
    assert rec.status == "OPEN"


@pytest.mark.asyncio
async def test_bulk_apply_with_mixed_outcomes(client_a: AsyncClient, db_session: AsyncSession, account_a: AdsAccount):
    """Bulk apply returns per-id results allowing partial success."""
    today = today_for(account_a.marketplace)

    camp = Campaign(ads_account_id=account_a.id, name="Camp Bulk", state="ENABLED", daily_budget=50.0, start_date=today - timedelta(days=5))
    db_session.add(camp)
    await db_session.flush()

    ag = AdGroup(campaign_id=camp.id, name="AG Bulk", default_bid=1.0)
    db_session.add(ag)
    await db_session.flush()

    # Kw 1: live bid matches evidence -> should SUCCEED
    kw1 = Keyword(ad_group_id=ag.id, keyword_text="kw valid", match_type="EXACT", bid=2.0, state="ENABLED")
    # Kw 2: live bid changed -> should FAIL (409)
    kw2 = Keyword(ad_group_id=ag.id, keyword_text="kw stale", match_type="EXACT", bid=3.0, state="ENABLED")
    db_session.add_all([kw1, kw2])
    await db_session.flush()

    rec1 = Recommendation(
        ads_account_id=account_a.id,
        rule_name="Rule 1",
        affected_parameter="TARGETING",
        entity_type="target",
        entity_id=kw1.id,
        entity_label="kw valid",
        campaign_id=camp.id,
        ad_group_id=ag.id,
        evidence={"metrics": {}, "matched": [], "current": {"bid": 2.0, "state": "ENABLED"}},
        suggested_actions=[{"type": "INCREASE_BID", "params": {"pct": 10}, "from": 2.0, "to": 2.2}],
        status="OPEN",
        dedupe_key="bulk:1",
    )
    rec2 = Recommendation(
        ads_account_id=account_a.id,
        rule_name="Rule 2",
        affected_parameter="TARGETING",
        entity_type="target",
        entity_id=kw2.id,
        entity_label="kw stale",
        campaign_id=camp.id,
        ad_group_id=ag.id,
        evidence={"metrics": {}, "matched": [], "current": {"bid": 1.0, "state": "ENABLED"}},  # stale! live is 3.0
        suggested_actions=[{"type": "INCREASE_BID", "params": {"pct": 10}, "from": 1.0, "to": 1.1}],
        status="OPEN",
        dedupe_key="bulk:2",
    )
    db_session.add_all([rec1, rec2])
    await db_session.commit()

    r = await client_a.post("/recommendations/bulk", json={"ids": [rec1.id, rec2.id], "action": "APPLY"})
    assert r.status_code == 200
    results = r.json()
    assert len(results) == 2

    res_map = {res["id"]: res for res in results}
    assert res_map[rec1.id]["success"] is True
    assert res_map[rec1.id]["error"] is None

    assert res_map[rec2.id]["success"] is False
    assert "This value changed since the analysis" in res_map[rec2.id]["error"]

    # Verify rec1 is APPLIED and kw1.bid was updated
    await db_session.refresh(rec1)
    await db_session.refresh(kw1)
    assert rec1.status == "APPLIED"
    assert kw1.bid == 2.2

    # Verify rec2 is still OPEN and kw2.bid was NOT updated
    await db_session.refresh(rec2)
    await db_session.refresh(kw2)
    assert rec2.status == "OPEN"
    assert kw2.bid == 3.0


@pytest.mark.asyncio
async def test_ownership_404s(client_b: AsyncClient, db_session: AsyncSession, account_a: AdsAccount):
    """Foreign account / user requests return 404."""
    today = today_for(account_a.marketplace)

    camp = Campaign(ads_account_id=account_a.id, name="Camp A", state="ENABLED", daily_budget=50.0, start_date=today - timedelta(days=5))
    db_session.add(camp)
    await db_session.flush()

    rec = Recommendation(
        ads_account_id=account_a.id,
        rule_name="Rule Account A",
        affected_parameter="CAMPAIGN",
        entity_type="campaign",
        entity_id=camp.id,
        entity_label=camp.name,
        campaign_id=camp.id,
        evidence={"metrics": {}, "matched": [], "current": {}},
        suggested_actions=[{"type": "PAUSE_CAMPAIGN", "params": {}}],
        status="OPEN",
        dedupe_key="own:1",
    )
    db_session.add(rec)
    await db_session.commit()

    # User B accessing Account A recommendations
    assert (await client_b.get(f"/recommendations/{rec.id}")).status_code == 404
    assert (await client_b.post(f"/recommendations/{rec.id}/apply")).status_code == 404
    assert (await client_b.post(f"/recommendations/{rec.id}/dismiss")).status_code == 404
    assert (await client_b.post(f"/recommendations/{rec.id}/reopen")).status_code == 404
    assert (await client_b.get(f"/recommendations?ads_account_id={account_a.id}")).status_code == 404
    assert (await client_b.get(f"/recommendations/summary?ads_account_id={account_a.id}")).status_code == 404
    assert (await client_b.post("/recommendations/run", json={"ads_account_id": account_a.id})).status_code == 404


@pytest.mark.asyncio
async def test_preview_writes_nothing(client_a: AsyncClient, db_session: AsyncSession, account_a: AdsAccount):
    """Preview evaluates draft rule and writes nothing to the database."""
    provider = MockAdsProvider(db_session)
    await provider.generate_for_new_connection(account_a.id)

    # Initial counts
    rec_count_before = (await db_session.execute(select(func.count(Recommendation.id)))).scalar()
    runs_count_before = (await db_session.execute(select(func.count(RecommendationRun.id)))).scalar()
    rules_count_before = (await db_session.execute(select(func.count(RecommendationRule.id)))).scalar()

    payload = {
        "ads_account_id": account_a.id,
        "rule": {
            "name": "Preview Draft Rule",
            "affected_parameter": "SEARCH_TERM",
            "date_filter": "LAST_30_DAYS",
            "min_clicks": 0,
            "conditions": [{"metric": "spend", "op": "GTE", "value": 10.0}],
            "actions": [{"type": "ADD_AS_NEGATIVE", "params": {"match_type": "NEGATIVE_EXACT", "level": "AD_GROUP"}}],
            "scope_mode": "ALL",
            "campaign_ids": [],
        },
    }

    r = await client_a.post("/recommendations/rules/preview", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data["match_count"] >= 1
    assert data["evaluated_entities"] >= 1
    assert len(data["sample"]) >= 1

    # Counts after preview must be identical
    rec_count_after = (await db_session.execute(select(func.count(Recommendation.id)))).scalar()
    runs_count_after = (await db_session.execute(select(func.count(RecommendationRun.id)))).scalar()
    rules_count_after = (await db_session.execute(select(func.count(RecommendationRule.id)))).scalar()

    assert rec_count_after == rec_count_before
    assert runs_count_after == runs_count_before
    assert rules_count_after == rules_count_before


@pytest.mark.asyncio
async def test_reopen_and_filtering_and_summary(client_a: AsyncClient, db_session: AsyncSession, account_a: AdsAccount):
    """
    Test reopen endpoint, summary endpoint, and list filtering with pagination & sort=impact.
    """
    today = today_for(account_a.marketplace)

    camp = Campaign(ads_account_id=account_a.id, name="Camp Filter", state="ENABLED", daily_budget=50.0, start_date=today - timedelta(days=5))
    db_session.add(camp)
    await db_session.flush()

    rec = Recommendation(
        ads_account_id=account_a.id,
        rule_name="Reopen Test Rule",
        affected_parameter="CAMPAIGN",
        entity_type="campaign",
        entity_id=camp.id,
        entity_label=camp.name,
        campaign_id=camp.id,
        evidence={
            "metrics": {"spend": 100.0, "orders": 0},
            "matched": [],
            "current": {"daily_budget": 50.0, "state": "ENABLED"},
            "projection": {"days": 30, "spend_in_period": 100.0, "monthly_equivalent": 100.0},
        },
        suggested_actions=[{"type": "PAUSE_CAMPAIGN", "params": {}}],
        status="OPEN",
        dedupe_key="reopen:1",
    )
    db_session.add(rec)
    await db_session.commit()

    # 1. Summary before dismiss
    r_sum = await client_a.get(f"/recommendations/summary?ads_account_id={account_a.id}")
    assert r_sum.status_code == 200
    assert r_sum.json()["open"] == 1

    # 2. Dismiss
    await client_a.post(f"/recommendations/{rec.id}/dismiss")
    r_sum2 = await client_a.get(f"/recommendations/summary?ads_account_id={account_a.id}")
    assert r_sum2.json()["open"] == 0
    assert r_sum2.json()["dismissed_30d"] == 1

    # 3. Reopen
    r_reopen = await client_a.post(f"/recommendations/{rec.id}/reopen")
    assert r_reopen.status_code == 200
    assert r_reopen.json()["status"] == "OPEN"
    assert r_reopen.json()["resolved_at"] is None

    # Cannot reopen if already OPEN
    r_reopen_again = await client_a.post(f"/recommendations/{rec.id}/reopen")
    assert r_reopen_again.status_code == 400

    # 4. List with sort=impact and filter by q
    r_list = await client_a.get(f"/recommendations?ads_account_id={account_a.id}&q=Filter&sort=impact")
    assert r_list.status_code == 200
    list_data = r_list.json()
    assert list_data["total_count"] == 1
    assert list_data["counts"]["OPEN"] == 1
    assert list_data["items"][0]["entity_label"] == camp.name
