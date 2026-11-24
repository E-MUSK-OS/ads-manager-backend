import asyncio
import json
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
from app.core.marketplace_rules import today_for
from app.models.ad_group import AdGroup
from app.models.ads_account import AdsAccount
from app.models.campaign import Campaign
from app.models.campaign_history import CampaignHistory
from app.models.keyword import Keyword
from app.models.negative_keyword import NegativeKeyword
from app.models.recommendation import Recommendation
from app.models.sync_run import SyncRun
from app.models.user import User
from app.providers.ads.amazon_provider import AmazonAdsProvider


_real_async_client = httpx.AsyncClient


def make_mock_client_factory(transport):
    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return _real_async_client(*args, **kwargs)
    return factory


@pytest_asyncio.fixture
async def real_style_account(db_session: AsyncSession, user_a: User, monkeypatch):
    """Account configured with non-dummy profile and encrypted refresh token."""
    test_key = crypto.generate_key()
    monkeypatch.setattr("app.config.settings.TOKEN_ENCRYPTION_KEY", test_key)
    monkeypatch.setattr("app.config.settings.AMAZON_LWA_CLIENT_ID", "mock_client_id")
    monkeypatch.setattr("app.config.settings.AMAZON_LWA_CLIENT_SECRET", "mock_client_secret")

    acc = AdsAccount(
        user_id=user_a.id,
        profile_id="amzn_profile_in_555",  # Real non-dummy profile
        marketplace="IN",
        is_mock=False,
        connection_status="connected",
        sync_status="idle",
        refresh_token_encrypted=crypto.encrypt("test_refresh_token_123", key=test_key),
    )
    db_session.add(acc)
    await db_session.commit()
    await db_session.refresh(acc)
    return acc


@pytest.mark.asyncio
async def test_apply_success_calls_amazon_and_updates_db_and_history(
    client_a: AsyncClient,
    db_session: AsyncSession,
    real_style_account: AdsAccount,
    monkeypatch,
):
    """
    R4.3 & R4.5:
    - Calls PUT /sp/keywords with application/vnd.spKeyword.v3+json
    - On 207 success, updates local Keyword.bid
    - Inserts CampaignHistory with entity_type='keyword', entity_id, recommendation_id
    - Marks recommendation status='APPLIED'
    """
    today = today_for(real_style_account.marketplace)
    camp = Campaign(
        ads_account_id=real_style_account.id,
        name="Camp R4 Success",
        amazon_campaign_id="amzn_c_99",
        state="ENABLED",
        daily_budget=500.0,
        start_date=today - timedelta(days=5),
    )
    db_session.add(camp)
    await db_session.flush()

    ag = AdGroup(campaign_id=camp.id, name="AG R4", default_bid=20.0)
    db_session.add(ag)
    await db_session.flush()

    kw = Keyword(
        ad_group_id=ag.id,
        keyword_text="wireless mouse",
        external_id="amzn_kw_101",
        match_type="EXACT",
        bid=20.0,
        state="ENABLED",
    )
    db_session.add(kw)
    await db_session.flush()

    rec = Recommendation(
        ads_account_id=real_style_account.id,
        rule_name="Reduce Bleeding Bid",
        affected_parameter="TARGETING",
        entity_type="target",
        entity_id=kw.id,
        entity_label="wireless mouse",
        campaign_id=camp.id,
        ad_group_id=ag.id,
        evidence={
            "metrics": {"clicks": 25, "orders": 0, "spend": 500.0},
            "matched": [],
            "current": {"bid": 20.0, "state": "ENABLED"},
        },
        suggested_actions=[{"type": "DECREASE_BID", "params": {"pct": 10}, "from": 20.0, "to": 18.0}],
        status="OPEN",
        dedupe_key="r4:success:1",
    )
    db_session.add(rec)
    await db_session.commit()

    # Mock LWA token and Amazon Ads endpoints
    monkeypatch.setattr(AmazonAdsProvider, "get_access_token", AsyncMock(return_value="mock_access_token"))

    amazon_call_received = []

    def mock_amazon_handler(request: httpx.Request) -> httpx.Response:
        url_path = request.url.path
        if "/sp/keywords" in url_path and request.method == "PUT":
            assert request.headers.get("Amazon-Advertising-API-Scope") == "amzn_profile_in_555"
            assert request.headers.get("Content-Type") == "application/vnd.spKeyword.v3+json"
            assert request.headers.get("Accept") == "application/vnd.spKeyword.v3+json"
            body = json.loads(request.content.decode("utf-8"))
            amazon_call_received.append(body)
            return httpx.Response(
                207,
                json={
                    "keywords": {
                        "success": [{"keywordId": "amzn_kw_101", "index": 0}],
                        "error": [],
                    }
                },
            )
        elif "/sp/keywords/list" in url_path:
            return httpx.Response(
                200,
                json={
                    "keywords": [
                        {"keywordId": "amzn_kw_101", "bid": 20.0, "state": "ENABLED"}
                    ]
                },
            )
        elif "/sp/campaigns/list" in url_path:
            return httpx.Response(200, json={"campaigns": []})
        return httpx.Response(404, json={"error": "Not Found"})

    transport = httpx.MockTransport(mock_amazon_handler)
    monkeypatch.setattr(httpx, "AsyncClient", make_mock_client_factory(transport))

    # Apply recommendation
    r = await client_a.post(f"/recommendations/{rec.id}/apply")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "APPLIED"

    # Verify Amazon call occurred with rounded money bid (18.0)
    assert len(amazon_call_received) == 1
    assert amazon_call_received[0]["keywords"][0]["keywordId"] == "amzn_kw_101"
    assert amazon_call_received[0]["keywords"][0]["bid"] == 18.0

    # Verify local DB keyword bid was updated
    await db_session.refresh(kw)
    assert kw.bid == 18.0

    # Verify CampaignHistory was created with entity_type, entity_id, recommendation_id
    history_res = await db_session.execute(
        select(CampaignHistory).where(CampaignHistory.recommendation_id == rec.id)
    )
    hist = history_res.scalars().first()
    assert hist is not None
    assert hist.entity_type == "keyword"
    assert hist.entity_id == kw.id
    assert hist.previous_value == "20.0"
    assert hist.new_value == "18.0"


@pytest.mark.asyncio
async def test_apply_partial_failure_amazon_207_error(
    client_a: AsyncClient,
    db_session: AsyncSession,
    real_style_account: AdsAccount,
    monkeypatch,
):
    """
    R4.3 & R4.7:
    - 2 recommendations submitted in bulk
    - Amazon 207 returns index 0 success, index 1 error
    - Item 0 applies cleanly and updates local DB
    - Item 1 fails, remains OPEN, error recorded, local DB NOT touched
    """
    today = today_for(real_style_account.marketplace)
    camp = Campaign(
        ads_account_id=real_style_account.id,
        name="Camp Partial",
        amazon_campaign_id="amzn_c_partial",
        state="ENABLED",
        daily_budget=500.0,
        start_date=today - timedelta(days=5),
    )
    db_session.add(camp)
    await db_session.flush()

    ag = AdGroup(campaign_id=camp.id, name="AG Partial", default_bid=20.0)
    db_session.add(ag)
    await db_session.flush()

    kw1 = Keyword(
        ad_group_id=ag.id,
        keyword_text="kw success",
        external_id="amzn_kw_1",
        match_type="EXACT",
        bid=20.0,
        state="ENABLED",
    )
    kw2 = Keyword(
        ad_group_id=ag.id,
        keyword_text="kw fail",
        external_id="amzn_kw_2",
        match_type="EXACT",
        bid=20.0,
        state="ENABLED",
    )
    db_session.add_all([kw1, kw2])
    await db_session.flush()

    rec1 = Recommendation(
        ads_account_id=real_style_account.id,
        rule_name="Rule 1",
        affected_parameter="TARGETING",
        entity_type="target",
        entity_id=kw1.id,
        entity_label="kw success",
        campaign_id=camp.id,
        ad_group_id=ag.id,
        evidence={"metrics": {}, "matched": [], "current": {"bid": 20.0, "state": "ENABLED"}},
        suggested_actions=[{"type": "DECREASE_BID", "params": {"pct": 10}, "from": 20.0, "to": 18.0}],
        status="OPEN",
        dedupe_key="partial:1",
    )
    rec2 = Recommendation(
        ads_account_id=real_style_account.id,
        rule_name="Rule 2",
        affected_parameter="TARGETING",
        entity_type="target",
        entity_id=kw2.id,
        entity_label="kw fail",
        campaign_id=camp.id,
        ad_group_id=ag.id,
        evidence={"metrics": {}, "matched": [], "current": {"bid": 20.0, "state": "ENABLED"}},
        suggested_actions=[{"type": "DECREASE_BID", "params": {"pct": 10}, "from": 20.0, "to": 18.0}],
        status="OPEN",
        dedupe_key="partial:2",
    )
    db_session.add_all([rec1, rec2])
    await db_session.commit()

    monkeypatch.setattr(AmazonAdsProvider, "get_access_token", AsyncMock(return_value="mock_access_token"))

    def mock_amazon_handler(request: httpx.Request) -> httpx.Response:
        url_path = request.url.path
        if "/sp/keywords" in url_path and request.method == "PUT":
            body = json.loads(request.content.decode("utf-8"))
            kw_items = body.get("keywords", [])
            # Return item 0 as success, item 1 as error
            success_list = []
            error_list = []
            for idx, item in enumerate(kw_items):
                if item["keywordId"] == "amzn_kw_1":
                    success_list.append({"keywordId": "amzn_kw_1", "index": idx})
                else:
                    error_list.append({
                        "keywordId": item["keywordId"],
                        "index": idx,
                        "code": "INVALID_BID",
                        "details": "Amazon rejected bid change: bid below threshold",
                    })
            return httpx.Response(
                207,
                json={"keywords": {"success": success_list, "error": error_list}},
            )
        elif "/sp/keywords/list" in url_path:
            return httpx.Response(
                200,
                json={
                    "keywords": [
                        {"keywordId": "amzn_kw_1", "bid": 20.0, "state": "ENABLED"},
                        {"keywordId": "amzn_kw_2", "bid": 20.0, "state": "ENABLED"},
                    ]
                },
            )
        elif "/sp/campaigns/list" in url_path:
            return httpx.Response(200, json={"campaigns": []})
        return httpx.Response(404, json={"error": "Not Found"})

    transport = httpx.MockTransport(mock_amazon_handler)
    monkeypatch.setattr(httpx, "AsyncClient", make_mock_client_factory(transport))

    r = await client_a.post("/recommendations/bulk", json={"ids": [rec1.id, rec2.id], "action": "APPLY", "confirm": True, "idempotency_key": "test_partial_1"})
    assert r.status_code == 200
    results = r.json()
    assert len(results) == 2

    res_map = {res["id"]: res for res in results}
    # Rec 1 succeeded
    assert res_map[rec1.id]["success"] is True
    assert res_map[rec1.id]["error"] is None
    await db_session.refresh(rec1)
    await db_session.refresh(kw1)
    assert rec1.status == "APPLIED"
    assert kw1.bid == 18.0

    # Rec 2 failed on Amazon
    assert res_map[rec2.id]["success"] is False
    assert "Amazon rejected bid change" in res_map[rec2.id]["error"]
    await db_session.refresh(rec2)
    await db_session.refresh(kw2)
    assert rec2.status == "OPEN"
    assert kw2.bid == 20.0  # NOT updated locally!


@pytest.mark.asyncio
async def test_apply_stale_item_check_aborts_write(
    client_a: AsyncClient,
    db_session: AsyncSession,
    real_style_account: AdsAccount,
    monkeypatch,
):
    """
    R4.4: Stale re-validation:
    If live value on Amazon changed since analysis, apply returns 409 and does not write.
    """
    today = today_for(real_style_account.marketplace)
    camp = Campaign(
        ads_account_id=real_style_account.id,
        name="Camp Stale Live",
        amazon_campaign_id="amzn_c_stale",
        state="ENABLED",
        daily_budget=500.0,
        start_date=today - timedelta(days=5),
    )
    db_session.add(camp)
    await db_session.flush()

    ag = AdGroup(campaign_id=camp.id, name="AG Stale Live", default_bid=20.0)
    db_session.add(ag)
    await db_session.flush()

    kw = Keyword(
        ad_group_id=ag.id,
        keyword_text="stale term",
        external_id="amzn_kw_stale_1",
        match_type="EXACT",
        bid=20.0,
        state="ENABLED",
    )
    db_session.add(kw)
    await db_session.flush()

    # Evidence recorded bid was 20.0
    rec = Recommendation(
        ads_account_id=real_style_account.id,
        rule_name="Rule Stale",
        affected_parameter="TARGETING",
        entity_type="target",
        entity_id=kw.id,
        entity_label="stale term",
        campaign_id=camp.id,
        ad_group_id=ag.id,
        evidence={"metrics": {}, "matched": [], "current": {"bid": 20.0, "state": "ENABLED"}},
        suggested_actions=[{"type": "DECREASE_BID", "params": {"pct": 10}, "from": 20.0, "to": 18.0}],
        status="OPEN",
        dedupe_key="stale:live:1",
    )
    db_session.add(rec)
    await db_session.commit()

    monkeypatch.setattr(AmazonAdsProvider, "get_access_token", AsyncMock(return_value="mock_access_token"))

    put_calls = []

    def mock_amazon_handler(request: httpx.Request) -> httpx.Response:
        url_path = request.url.path
        if "/sp/keywords/list" in url_path:
            # Amazon reports bid was changed outside the app to 35.0!
            return httpx.Response(
                200,
                json={
                    "keywords": [
                        {"keywordId": "amzn_kw_stale_1", "bid": 35.0, "state": "ENABLED"}
                    ]
                },
            )
        elif "/sp/keywords" in url_path and request.method == "PUT":
            put_calls.append(request)
            return httpx.Response(200, json={})
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(mock_amazon_handler)
    monkeypatch.setattr(httpx, "AsyncClient", make_mock_client_factory(transport))

    r = await client_a.post(f"/recommendations/{rec.id}/apply")
    assert r.status_code == 409
    assert "This value changed since the analysis" in r.json()["detail"]

    # Verify no write occurred
    assert len(put_calls) == 0

    await db_session.refresh(rec)
    assert rec.status == "OPEN"


@pytest.mark.asyncio
async def test_apply_429_retry_and_backoff(
    client_a: AsyncClient,
    db_session: AsyncSession,
    real_style_account: AdsAccount,
    monkeypatch,
):
    """
    R4.7: Provider request_api transparently retries on 429 Too Many Requests with Retry-After.
    """
    today = today_for(real_style_account.marketplace)
    camp = Campaign(
        ads_account_id=real_style_account.id,
        name="Camp 429 Test",
        amazon_campaign_id="amzn_c_429",
        state="ENABLED",
        daily_budget=500.0,
        start_date=today - timedelta(days=5),
    )
    db_session.add(camp)
    await db_session.flush()

    ag = AdGroup(campaign_id=camp.id, name="AG 429", default_bid=20.0)
    db_session.add(ag)
    await db_session.flush()

    kw = Keyword(
        ad_group_id=ag.id,
        keyword_text="rate limited kw",
        external_id="amzn_kw_429",
        match_type="EXACT",
        bid=20.0,
        state="ENABLED",
    )
    db_session.add(kw)
    await db_session.flush()

    rec = Recommendation(
        ads_account_id=real_style_account.id,
        rule_name="Rule 429",
        affected_parameter="TARGETING",
        entity_type="target",
        entity_id=kw.id,
        entity_label="rate limited kw",
        campaign_id=camp.id,
        ad_group_id=ag.id,
        evidence={"metrics": {}, "matched": [], "current": {"bid": 20.0, "state": "ENABLED"}},
        suggested_actions=[{"type": "DECREASE_BID", "params": {"pct": 10}, "from": 20.0, "to": 18.0}],
        status="OPEN",
        dedupe_key="rec:429:1",
    )
    db_session.add(rec)
    await db_session.commit()

    monkeypatch.setattr(AmazonAdsProvider, "get_access_token", AsyncMock(return_value="mock_access_token"))

    attempts = {"count": 0}

    def mock_amazon_handler(request: httpx.Request) -> httpx.Response:
        url_path = request.url.path
        if "/sp/keywords/list" in url_path:
            return httpx.Response(
                200,
                json={"keywords": [{"keywordId": "amzn_kw_429", "bid": 20.0, "state": "ENABLED"}]},
            )
        elif "/sp/keywords" in url_path and request.method == "PUT":
            attempts["count"] += 1
            if attempts["count"] == 1:
                # Return 429 on first try with 0.05s Retry-After
                return httpx.Response(429, headers={"Retry-After": "0.05"}, text="Rate exceeded")
            return httpx.Response(
                207,
                json={"keywords": {"success": [{"keywordId": "amzn_kw_429", "index": 0}], "error": []}},
            )
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(mock_amazon_handler)
    monkeypatch.setattr(httpx, "AsyncClient", make_mock_client_factory(transport))

    r = await client_a.post(f"/recommendations/{rec.id}/apply")
    assert r.status_code == 200
    assert attempts["count"] == 2  # Retried and succeeded!


@pytest.mark.asyncio
async def test_apply_dummy_profile_rejected(
    client_a: AsyncClient,
    db_session: AsyncSession,
    user_a: User,
):
    """
    R4.3 & Extra: Apply refuses on dummy_ profiles with HTTP 400.
    """
    acc = AdsAccount(
        user_id=user_a.id,
        profile_id="dummy_profile_in_999",  # dummy profile!
        marketplace="IN",
        is_mock=False,
    )
    db_session.add(acc)
    await db_session.commit()

    camp = Campaign(ads_account_id=acc.id, name="Camp Dummy", state="ENABLED", daily_budget=500.0)
    db_session.add(camp)
    await db_session.flush()

    rec = Recommendation(
        ads_account_id=acc.id,
        rule_name="Rule Dummy",
        affected_parameter="CAMPAIGN",
        entity_type="campaign",
        entity_id=camp.id,
        entity_label=camp.name,
        campaign_id=camp.id,
        evidence={"metrics": {}, "matched": [], "current": {"daily_budget": 500.0, "state": "ENABLED"}},
        suggested_actions=[{"type": "INCREASE_BUDGET", "params": {"pct": 10}, "from": 500.0, "to": 550.0}],
        status="OPEN",
        dedupe_key="rec:dummy:1",
    )
    db_session.add(rec)
    await db_session.commit()

    r = await client_a.post(f"/recommendations/{rec.id}/apply")
    assert r.status_code == 400
    assert "dummy profile" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_apply_running_sync_blocked(
    client_a: AsyncClient,
    db_session: AsyncSession,
    real_style_account: AdsAccount,
):
    """
    Q9: Block Apply while a sync is running for that account (409 Conflict).
    """
    camp = Campaign(ads_account_id=real_style_account.id, name="Camp Sync", state="ENABLED", daily_budget=500.0)
    db_session.add(camp)
    await db_session.flush()

    rec = Recommendation(
        ads_account_id=real_style_account.id,
        rule_name="Rule Sync",
        affected_parameter="CAMPAIGN",
        entity_type="campaign",
        entity_id=camp.id,
        entity_label=camp.name,
        campaign_id=camp.id,
        evidence={"metrics": {}, "matched": [], "current": {"daily_budget": 500.0, "state": "ENABLED"}},
        suggested_actions=[{"type": "INCREASE_BUDGET", "params": {"pct": 10}, "from": 500.0, "to": 550.0}],
        status="OPEN",
        dedupe_key="rec:sync:1",
    )
    db_session.add(rec)

    # Active running sync
    active_run = SyncRun(
        ads_account_id=real_style_account.id,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(active_run)
    await db_session.commit()

    r = await client_a.post(f"/recommendations/{rec.id}/apply")
    assert r.status_code == 409
    assert "synchronization is currently running" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_apply_reauth_required_hint(
    client_a: AsyncClient,
    db_session: AsyncSession,
    real_style_account: AdsAccount,
    monkeypatch,
):
    """
    R4.7: Invalid LWA token refresh returns 401 with clear reauth required hint.
    """
    camp = Campaign(ads_account_id=real_style_account.id, name="Camp Reauth", state="ENABLED", daily_budget=500.0)
    db_session.add(camp)
    await db_session.flush()

    rec = Recommendation(
        ads_account_id=real_style_account.id,
        rule_name="Rule Reauth",
        affected_parameter="CAMPAIGN",
        entity_type="campaign",
        entity_id=camp.id,
        entity_label=camp.name,
        campaign_id=camp.id,
        evidence={"metrics": {}, "matched": [], "current": {"daily_budget": 500.0, "state": "ENABLED"}},
        suggested_actions=[{"type": "INCREASE_BUDGET", "params": {"pct": 10}, "from": 500.0, "to": 550.0}],
        status="OPEN",
        dedupe_key="rec:reauth:1",
    )
    db_session.add(rec)
    await db_session.commit()

    # Clear memory cache so token refresh is invoked
    AmazonAdsProvider._access_token_cache.clear()

    def mock_lwa_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": "invalid_grant", "error_description": "The refresh token has expired."},
        )

    transport = httpx.MockTransport(mock_lwa_handler)
    monkeypatch.setattr(httpx, "AsyncClient", make_mock_client_factory(transport))

    r = await client_a.post(f"/recommendations/{rec.id}/apply")
    assert r.status_code == 401
    assert "reconnect your account" in r.json()["detail"].lower()

    await db_session.refresh(real_style_account)
    assert real_style_account.connection_status == "reauth_required"


@pytest.mark.asyncio
async def test_apply_already_applied_item_refused(
    client_a: AsyncClient,
    db_session: AsyncSession,
    account_a: AdsAccount,
):
    """
    R4.7: Calling apply on an already APPLIED recommendation returns 400.
    """
    camp = Campaign(ads_account_id=account_a.id, name="Camp Applied", state="ENABLED", daily_budget=500.0)
    db_session.add(camp)
    await db_session.flush()

    rec = Recommendation(
        ads_account_id=account_a.id,
        rule_name="Rule Applied",
        affected_parameter="CAMPAIGN",
        entity_type="campaign",
        entity_id=camp.id,
        entity_label=camp.name,
        campaign_id=camp.id,
        evidence={"metrics": {}, "matched": [], "current": {"daily_budget": 500.0, "state": "ENABLED"}},
        suggested_actions=[{"type": "INCREASE_BUDGET", "params": {"pct": 10}, "from": 500.0, "to": 550.0}],
        status="APPLIED",  # already applied
        dedupe_key="rec:applied:1",
    )
    db_session.add(rec)
    await db_session.commit()

    r = await client_a.post(f"/recommendations/{rec.id}/apply")
    assert r.status_code == 400
    assert "Cannot apply recommendation in APPLIED status" in r.json()["detail"]
