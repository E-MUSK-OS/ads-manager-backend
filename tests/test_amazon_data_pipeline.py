import asyncio
import gzip
import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select

from app.core import crypto
from app.models.ads_account import AdsAccount
from app.models.campaign import Campaign
from app.models.metric_daily import MetricDaily
from app.models.placement_metric_daily import PlacementMetricDaily
from app.models.sync_run import SyncRun
from app.providers.ads.amazon_provider import (
    MAX_REPORT_LOOKBACK_DAYS,
    MAX_REPORT_WINDOW_DAYS,
    AmazonAdsProvider,
    split_date_range,
)


@pytest.fixture(autouse=True)
def setup_encryption_key(monkeypatch):
    test_key = crypto.generate_key()
    monkeypatch.setattr("app.config.settings.TOKEN_ENCRYPTION_KEY", test_key)
    monkeypatch.setattr("app.providers.ads.amazon_provider.settings.TOKEN_ENCRYPTION_KEY", test_key)
    monkeypatch.setattr("app.providers.ads.amazon_provider.settings.AMAZON_LWA_CLIENT_ID", "mock_client_id")
    monkeypatch.setattr("app.providers.ads.amazon_provider.settings.AMAZON_LWA_CLIENT_SECRET", "mock_client_secret")
    return test_key


_real_async_client = httpx.AsyncClient


def make_mock_client_factory(transport):
    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return _real_async_client(*args, **kwargs)
    return factory


@pytest_asyncio.fixture
async def mock_account(db_session, user_a, setup_encryption_key):
    acc = AdsAccount(
        user_id=user_a.id,
        profile_id="amzn_profile_in_123",
        marketplace="IN",
        is_mock=False,
        connection_status="connected",
        sync_status="idle",
        refresh_token_encrypted=crypto.encrypt("test_refresh_token_123", key=setup_encryption_key),
        region="eu",
        amazon_account_name="Test Seller India",
    )
    db_session.add(acc)
    await db_session.commit()
    await db_session.refresh(acc)
    return acc


# ---------------------------------------------------------------------------
# 1. Paging over SP v3 Campaigns List
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_paging_sp_campaigns(db_session, mock_account, monkeypatch):
    provider = AmazonAdsProvider(db_session)
    monkeypatch.setattr(provider, "get_access_token", AsyncMock(return_value="mock_access_token"))

    def mock_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Amazon-Advertising-API-Scope") == "amzn_profile_in_123"
        assert request.headers.get("Authorization") == "Bearer mock_access_token"

        req_body = json.loads(request.content.decode("utf-8"))
        if not req_body.get("nextToken"):
            # Page 1: returns 2 campaigns with nextToken
            return httpx.Response(
                200,
                json={
                    "campaigns": [
                        {
                            "campaignId": "1001",
                            "name": "SP Campaign 1",
                            "state": "ENABLED",
                            "targetingType": "MANUAL",
                            "startDate": "2026-01-01",
                            "budget": {"budget": 500.0, "budgetType": "DAILY"},
                            "dynamicBidding": {
                                "strategy": "LEGACY_FOR_SALES",
                                "placementBidding": [{"placement": "PLACEMENT_TOP", "percentage": 50}],
                            },
                            "extendedData": {"servingStatus": "CAMPAIGN_DELIVERING"},
                        },
                        {
                            "campaignId": "1002",
                            "name": "SP Campaign 2",
                            "state": "PAUSED",
                            "targetingType": "AUTO",
                            "startDate": "2026-01-02",
                            "budget": {"budget": 300.0, "budgetType": "DAILY"},
                            "dynamicBidding": {"strategy": "DOWN_ONLY"},
                            "extendedData": {"servingStatus": "CAMPAIGN_OUT_OF_BUDGET"},
                        },
                    ],
                    "nextToken": "page_2_token",
                },
            )
        elif req_body.get("nextToken") == "page_2_token":
            # Page 2: returns 1 campaign, no nextToken
            return httpx.Response(
                200,
                json={
                    "campaigns": [
                        {
                            "campaignId": "1003",
                            "name": "SP Campaign 3",
                            "state": "ARCHIVED",
                            "targetingType": "MANUAL",
                            "startDate": "2026-01-03",
                            "budget": {"budget": 200.0, "budgetType": "DAILY"},
                            "dynamicBidding": {"strategy": "FIXED"},
                            "extendedData": {"servingStatus": "CAMPAIGN_ARCHIVED"},
                        }
                    ],
                    "nextToken": None,
                },
            )
        return httpx.Response(400, json={"error": "invalid_page"})

    transport = httpx.MockTransport(mock_handler)
    monkeypatch.setattr(httpx, "AsyncClient", make_mock_client_factory(transport))

    campaigns = await provider.fetch_sp_campaigns(mock_account)
    assert len(campaigns) == 3

    c1 = next(c for c in campaigns if c.amazon_campaign_id == "1001")
    assert c1.name == "SP Campaign 1"
    assert c1.state == "ENABLED"
    assert c1.delivery_status == "DELIVERING"
    assert c1.daily_budget == 500.0
    assert c1.bidding_strategy == "LEGACY_FOR_SALES"
    assert c1.placement_top_of_search_pct == 50

    c2 = next(c for c in campaigns if c.amazon_campaign_id == "1002")
    assert c2.delivery_status == "OUT_OF_BUDGET"

    c3 = next(c for c in campaigns if c.amazon_campaign_id == "1003")
    assert c3.state == "ARCHIVED"


# ---------------------------------------------------------------------------
# 2. Async Report Creation, Polling & Decompression
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_report_polling_success(db_session, mock_account, monkeypatch):
    provider = AmazonAdsProvider(db_session)
    monkeypatch.setattr(provider, "get_access_token", AsyncMock(return_value="mock_access_token"))

    today = date.today()
    sample_date = today - timedelta(days=2)
    poll_count = 0
    sample_records = [
        {
            "campaignId": "1001",
            "date": sample_date.strftime("%Y-%m-%d"),
            "impressions": 1200,
            "clicks": 45,
            "cost": 220.50,
            "purchases7d": 4,
            "sales7d": 1850.00,
        }
    ]
    compressed_gz = gzip.compress(json.dumps(sample_records).encode("utf-8"))

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        path = request.url.path

        if path.endswith("/reporting/reports") and request.method == "POST":
            return httpx.Response(200, json={"reportId": "rep_999", "status": "PENDING"})

        elif "/reporting/reports/rep_999" in path and request.method == "GET":
            poll_count += 1
            if poll_count < 2:
                return httpx.Response(200, json={"reportId": "rep_999", "status": "PROCESSING"})
            else:
                return httpx.Response(
                    200,
                    json={
                        "reportId": "rep_999",
                        "status": "COMPLETED",
                        "url": "https://s3.mock.amazon/reports/rep_999.json.gz",
                    },
                )

        elif "reports/rep_999.json.gz" in str(request.url):
            return httpx.Response(200, content=compressed_gz, headers={"Content-Type": "application/gzip"})

        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)
    monkeypatch.setattr(httpx, "AsyncClient", make_mock_client_factory(transport))
    # Faster polling in tests
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))

    records = await provider.request_and_download_report(
        mock_account, start_date=sample_date, end_date=sample_date
    )

    assert len(records) == 1
    assert records[0]["campaignId"] == "1001"
    assert records[0]["impressions"] == 1200
    assert records[0]["sales7d"] == 1850.00
    assert poll_count >= 2


# ---------------------------------------------------------------------------
# 3. Report Polling Failure Handling
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_report_polling_failure(db_session, mock_account, monkeypatch):
    provider = AmazonAdsProvider(db_session)
    monkeypatch.setattr(provider, "get_access_token", AsyncMock(return_value="mock_access_token"))

    def mock_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/reporting/reports") and request.method == "POST":
            return httpx.Response(200, json={"reportId": "rep_failed", "status": "PENDING"})
        elif "/reporting/reports/rep_failed" in path:
            return httpx.Response(
                200,
                json={
                    "reportId": "rep_failed",
                    "status": "FAILED",
                    "failureReason": "Report generation service error",
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)
    monkeypatch.setattr(httpx, "AsyncClient", make_mock_client_factory(transport))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))

    today = date.today()
    with pytest.raises(HTTPException) as exc_info:
        await provider.request_and_download_report(
            mock_account, start_date=today - timedelta(days=5), end_date=today - timedelta(days=1)
        )
    assert exc_info.value.status_code == 502
    assert "Report generation service error" in exc_info.value.detail


# ---------------------------------------------------------------------------
# 4. Exponential Backoff and Retry-After on 429
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_exponential_backoff_and_retry_after(db_session, mock_account, monkeypatch):
    provider = AmazonAdsProvider(db_session)
    monkeypatch.setattr(provider, "get_access_token", AsyncMock(return_value="mock_access_token"))

    attempts = 0
    sleep_calls = []

    async def mock_sleep(d):
        sleep_calls.append(d)

    monkeypatch.setattr(asyncio, "sleep", mock_sleep)

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "1.5"}, text="Rate Limit Exceeded")
        return httpx.Response(200, json={"status": "ok"})

    transport = httpx.MockTransport(mock_handler)
    monkeypatch.setattr(httpx, "AsyncClient", make_mock_client_factory(transport))

    resp = await provider.request_api(mock_account, "GET", "/test/path")
    assert resp.status_code == 200
    assert attempts == 2
    assert 1.5 in sleep_calls


# ---------------------------------------------------------------------------
# 5. Date Window Splitting (Max 31 Days per Request)
# ---------------------------------------------------------------------------
def test_date_window_splitting():
    # 70 days range must split into 3 chunks (31 + 31 + 8)
    start = date(2026, 1, 1)
    end = start + timedelta(days=69)  # 70 days total
    chunks = split_date_range(start, end, max_chunk_days=31)
    assert len(chunks) == 3

    assert chunks[0] == (date(2026, 1, 1), date(2026, 1, 31))
    assert chunks[1] == (date(2026, 2, 1), date(2026, 3, 3))
    assert chunks[2][1] == end

    # Exactly 31 days -> 1 chunk
    c_31 = split_date_range(start, start + timedelta(days=30), max_chunk_days=31)
    assert len(c_31) == 1


# ---------------------------------------------------------------------------
# 6. Idempotent Metric Upsert
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_idempotent_metric_upsert(db_session, mock_account):
    await db_session.commit()
    await db_session.refresh(mock_account)

    provider = AmazonAdsProvider(db_session)
    camp = Campaign(
        ads_account_id=mock_account.id,
        amazon_campaign_id="amzn_1001",
        external_id="amzn_1001",
        name="Test Campaign",
        daily_budget=500.0,
    )
    db_session.add(camp)
    await db_session.commit()
    await db_session.refresh(camp)

    records_day1 = [
        {
            "campaignId": "amzn_1001",
            "date": "2026-02-10",
            "impressions": 1000,
            "clicks": 50,
            "cost": 150.0,
            "purchases7d": 3,
            "sales7d": 1200.0,
        }
    ]

    # Ingest day 1
    c1 = await provider.upsert_report_metrics(mock_account, [camp], records_day1)
    assert c1 == 1

    m1_res = await db_session.execute(
        select(MetricDaily).where(MetricDaily.entity_id == camp.id, MetricDaily.date == date(2026, 2, 10))
    )
    m1 = m1_res.scalars().all()
    assert len(m1) == 1
    assert m1[0].clicks == 50
    assert m1[0].spend == 150.0

    # Ingest corrected/updated metrics for the exact same day
    records_day1_updated = [
        {
            "campaignId": "amzn_1001",
            "date": "2026-02-10",
            "impressions": 1200,
            "clicks": 60,
            "cost": 180.0,
            "purchases7d": 4,
            "sales7d": 1600.0,
        }
    ]
    c2 = await provider.upsert_report_metrics(mock_account, [camp], records_day1_updated)
    assert c2 == 1

    # Verify no duplicate row created, values updated cleanly
    m2_res = await db_session.execute(
        select(MetricDaily).where(MetricDaily.entity_id == camp.id, MetricDaily.date == date(2026, 2, 10))
    )
    m2 = m2_res.scalars().all()
    assert len(m2) == 1
    assert m2[0].clicks == 60
    assert m2[0].spend == 180.0
    assert m2[0].sales == 1600.0

    # Verify placement metrics were also populated and updated
    p_res = await db_session.execute(
        select(PlacementMetricDaily).where(
            PlacementMetricDaily.campaign_id == camp.id, PlacementMetricDaily.date == date(2026, 2, 10)
        )
    )
    placements = p_res.scalars().all()
    assert len(placements) == 3
    assert sum(p.clicks for p in placements) == 60
    assert round(sum(p.spend for p in placements), 2) == 180.0


# ---------------------------------------------------------------------------
# 7. Ownership Isolation & Tenant Security
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ownership_isolation_sync(client_b, mock_account):
    # User B cannot sync User A's account
    resp_sync = await client_b.post(f"/ads-accounts/{mock_account.id}/sync")
    assert resp_sync.status_code == 404

    # User B cannot query sync status of User A's account
    resp_status = await client_b.get(f"/ads-accounts/{mock_account.id}/sync-status")
    assert resp_status.status_code == 404


# ---------------------------------------------------------------------------
# 8. Concurrency Lock & Cooldown
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sync_concurrency_lock_and_cooldown(client_a, db_session, mock_account):
    # Set sync_status='syncing' with an active run
    mock_account.sync_status = "syncing"
    run = SyncRun(
        ads_account_id=mock_account.id,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(run)
    await db_session.commit()

    resp = await client_a.post(f"/ads-accounts/{mock_account.id}/sync")
    assert resp.status_code == 409
    assert "already running" in resp.json()["detail"]

    # Cooldown check: sync completed 30 seconds ago
    mock_account.sync_status = "idle"
    mock_account.last_synced_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    await db_session.commit()

    resp_cooldown = await client_a.post(f"/ads-accounts/{mock_account.id}/sync")
    assert resp_cooldown.status_code == 429
    assert "cooldown active" in resp_cooldown.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 9. invalid_grant Sets reauth_required
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_invalid_grant_sets_reauth_required(db_session, mock_account, monkeypatch):
    provider = AmazonAdsProvider(db_session)

    def mock_lwa_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": "invalid_grant", "error_description": "The refresh token has expired or is invalid."},
        )

    transport = httpx.MockTransport(mock_lwa_handler)
    monkeypatch.setattr(httpx, "AsyncClient", make_mock_client_factory(transport))

    with pytest.raises(HTTPException) as exc_info:
        await provider.get_access_token(mock_account)

    assert exc_info.value.status_code == 401
    await db_session.refresh(mock_account)
    assert mock_account.connection_status == "reauth_required"
    assert mock_account.sync_status == "failed"
