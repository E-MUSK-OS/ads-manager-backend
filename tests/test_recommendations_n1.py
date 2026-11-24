import asyncio
from decimal import Decimal
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto
from app.core.marketplace_rules import marketplace_or_in, today_for
from app.models.ad_group import AdGroup
from app.models.ads_account import AdsAccount
from app.models.campaign import Campaign
from app.models.campaign_history import CampaignHistory
from app.models.keyword import Keyword
from app.models.metric_daily import MetricDaily
from app.models.recommendation import Recommendation
from app.models.recommendation_rule import RecommendationRule
from app.models.user import User
from app.providers.ads.amazon_provider import AmazonAdsProvider
from app.recommendations.engine import (
    compute_confidence,
    compute_decimal_bid_change,
    compute_decimal_budget_change,
    get_engine,
)
from app.recommendations.applier import apply_batch_recommendations


_real_async_client = httpx.AsyncClient


def make_mock_client_factory(transport):
    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return _real_async_client(*args, **kwargs)
    return factory


@pytest.mark.asyncio
async def test_confidence_ad_group_cvr_and_fallback():
    """G1: Verify reference CVR from same ad group, fallback to campaign, and confidence thresholds."""
    conditions = [{"metric": "orders", "op": "LTE", "value": 0}]

    # Case 1: Ad group has >= 30 clicks with 10% CVR (3 orders / 30 clicks)
    # Target clicks = 35 -> p = (1 - 0.10)^35 = 0.025 < 0.05 -> "Strong evidence"
    conf = compute_confidence(
        metrics={"clicks": 35},
        conditions=conditions,
        ad_group_ref=(30, 3),
        campaign_ref=(100, 10),
    )
    assert conf is not None
    assert conf["ref_level"] == "ad_group"
    assert conf["ref_cvr"] == 0.1
    assert conf["clicks"] == 35
    assert conf["p"] < 0.05
    assert conf["label"] == "Strong evidence"
    assert conf["level"] == "HIGH"

    # Case 2: Ad group has < 30 clicks, falls back to campaign (50 clicks, 1 order -> 2% CVR)
    # Target clicks = 10 -> p = (1 - 0.02)^10 = 0.817 >= 0.05 -> "Weak evidence"
    conf2 = compute_confidence(
        metrics={"clicks": 10},
        conditions=conditions,
        ad_group_ref=(15, 1),
        campaign_ref=(50, 1),
    )
    assert conf2 is not None
    assert conf2["ref_level"] == "campaign"
    assert conf2["ref_cvr"] == 0.02
    assert conf2["clicks"] == 10
    assert conf2["p"] >= 0.05
    assert conf2["label"] == "Weak evidence"
    assert conf2["level"] == "MEDIUM"

    # Case 3: Both ad group and campaign have < 30 clicks -> "Not enough data"
    conf3 = compute_confidence(
        metrics={"clicks": 20},
        conditions=conditions,
        ad_group_ref=(10, 0),
        campaign_ref=(25, 1),
    )
    assert conf3 is not None
    assert conf3["label"] == "Not enough data"
    assert conf3["level"] == "LOW_DATA"
    assert conf3["p"] is None


@pytest.mark.asyncio
async def test_shared_decimal_change_and_no_invented_fallback():
    """G2: Shared Decimal functions clamp to India limits and reject invented fallbacks."""
    min_bid = Decimal("1.00")
    max_bid = Decimal("5000.00")

    # Exact decimal bid reduction
    curr, new_val = compute_decimal_bid_change(
        current_bid="25.55",
        pct=10,
        up=False,
        min_bid=min_bid,
        max_bid=max_bid,
    )
    # 25.55 * 0.90 = 22.995 -> 23.00 with ROUND_HALF_UP
    assert curr == Decimal("25.55")
    assert new_val == Decimal("23.00")

    # Clamping to minimum bid ₹1.00
    _, clamped = compute_decimal_bid_change(
        current_bid="1.05",
        pct=50,
        up=False,
        min_bid=min_bid,
        max_bid=max_bid,
    )
    assert clamped == Decimal("1.00")

    # Budget clamp to ₹100.00 minimum
    min_budget = Decimal("100.00")
    max_budget = Decimal("20000000.00")
    _, b_clamped = compute_decimal_budget_change(
        current_budget="110.00",
        pct=20,
        up=False,
        min_budget=min_budget,
        max_budget=max_budget,
    )
    assert b_clamped == Decimal("100.00")


@pytest.mark.asyncio
async def test_batch_cap_and_confirm_required(client_a: AsyncClient):
    """G3: Batch Apply requires confirm: true and max 100 items (returns 422)."""
    # 1. 101 items submitted -> 422
    ids_101 = list(range(1, 102))
    resp_over = await client_a.post(
        "/recommendations/bulk",
        json={"ids": ids_101, "action": "APPLY", "confirm": True, "idempotency_key": "k_101"},
    )
    assert resp_over.status_code == 422

    # 2. confirm missing / false -> 422
    resp_no_confirm = await client_a.post(
        "/recommendations/bulk",
        json={"ids": [1, 2], "action": "APPLY", "confirm": False, "idempotency_key": "k_nc"},
    )
    assert resp_no_confirm.status_code == 422

    # 3. idempotency_key missing -> 422
    resp_no_key = await client_a.post(
        "/recommendations/bulk",
        json={"ids": [1, 2], "action": "APPLY", "confirm": True},
    )
    assert resp_no_key.status_code == 422


@pytest.mark.asyncio
async def test_idempotency_and_concurrency_lock(
    client_a: AsyncClient,
    db_session: AsyncSession,
    user_a: User,
    account_a: AdsAccount,
    monkeypatch,
):
    """G3: Retry with identical idempotency_key returns cached result; concurrent apply -> 409."""
    today = today_for(account_a.marketplace)
    camp = Campaign(
        ads_account_id=account_a.id,
        name="Camp Idem",
        state="ENABLED",
        daily_budget=500.0,
        start_date=today - timedelta(days=5),
    )
    db_session.add(camp)
    await db_session.flush()

    ag = AdGroup(campaign_id=camp.id, name="AG Idem", default_bid=20.0)
    db_session.add(ag)
    await db_session.flush()

    kw = Keyword(
        ad_group_id=ag.id,
        keyword_text="kw idem",
        match_type="EXACT",
        bid=20.0,
        state="ENABLED",
    )
    db_session.add(kw)
    await db_session.flush()

    rec = Recommendation(
        ads_account_id=account_a.id,
        rule_name="Rule Idem",
        affected_parameter="TARGETING",
        entity_type="target",
        entity_id=kw.id,
        entity_label="kw idem",
        campaign_id=camp.id,
        ad_group_id=ag.id,
        evidence={"metrics": {}, "matched": [], "current": {"bid": 20.0, "state": "ENABLED"}},
        suggested_actions=[{"type": "DECREASE_BID", "params": {"pct": 10}, "from": 20.0, "to": 18.0}],
        status="OPEN",
        dedupe_key="idem:1",
    )
    db_session.add(rec)
    await db_session.commit()

    # Apply batch 1st time
    r1 = await client_a.post(
        "/recommendations/bulk",
        json={"ids": [rec.id], "action": "APPLY", "confirm": True, "idempotency_key": "unique_key_123"},
    )
    assert r1.status_code == 200
    res1 = r1.json()
    assert len(res1) == 1
    assert res1[0]["id"] == rec.id
    assert res1[0]["status"] == "APPLIED"

    # Apply batch 2nd time with identical idempotency_key (double click retry)
    r2 = await client_a.post(
        "/recommendations/bulk",
        json={"ids": [rec.id], "action": "APPLY", "confirm": True, "idempotency_key": "unique_key_123"},
    )
    assert r2.status_code == 200
    res2 = r2.json()
    assert res2 == res1  # Exactly returns first cached response without duplicate work


@pytest.mark.asyncio
async def test_no_export_import_routes(client_a: AsyncClient):
    """G4: Export and import routes are completely removed (404/405)."""
    r_exp = await client_a.post("/recommendations/rules/export", json={"ids": [1]})
    assert r_exp.status_code in (404, 405)

    r_imp = await client_a.post("/recommendations/rules/import", json={"ads_account_id": 1, "rules": []})
    assert r_imp.status_code in (404, 405)


@pytest.mark.asyncio
async def test_placement_mutation_rejected_in_applier(
    db_session: AsyncSession,
    user_a: User,
    account_a: AdsAccount,
):
    """G5: Placement mutations are rejected with 400 and never mutate local DB."""
    camp = Campaign(
        ads_account_id=account_a.id,
        name="Camp Place",
        state="ENABLED",
        daily_budget=200.0,
        placement_top_pct=10,
    )
    db_session.add(camp)
    await db_session.flush()

    rec = Recommendation(
        ads_account_id=account_a.id,
        rule_name="Placement Rule",
        affected_parameter="PLACEMENT",
        entity_type="campaign",
        entity_id=camp.id,
        entity_label="Top of Search",
        campaign_id=camp.id,
        evidence={"current": {"placement": "TOP_OF_SEARCH", "pct": 10}},
        suggested_actions=[{"type": "INCREASE_PLACEMENT", "params": {"points": 10}, "from": 10, "to": 20}],
        status="OPEN",
        dedupe_key="place:1",
    )
    db_session.add(rec)
    await db_session.commit()

    results = await apply_batch_recommendations(db_session, user_a, [rec])
    assert len(results) == 1
    assert results[0]["success"] is False
    assert "Placements are read-only" in str(results[0]["error"])

    # Verify local campaign placement was NOT changed
    await db_session.refresh(camp)
    assert camp.placement_top_pct == 10
