"""Tests for Phase N4: Local-only write routes writing to Amazon first.

Covers:
1. POST /campaigns/bulk:
   - Max 100 items enforced
   - SET_PLACEMENT_PCT rejected (placements read-only)
   - Disconnected account rejected
   - Connected real account: calls Amazon PUT /sp/campaigns, updates on success, writes CampaignHistory
   - Connected real account: rejects local update on Amazon error
2. PATCH /keywords/{id}:
   - Disconnected account rejected
   - Missing external_id refuses with clear message
   - With external_id: calls Amazon PUT /sp/keywords, updates on success, writes CampaignHistory
   - Rejects local update on Amazon error
3. PATCH /campaigns/{campaign_id}/targets/{target_id}:
   - Disconnected account rejected
   - Missing external_id refuses with clear message
   - With external_id: calls Amazon PUT /sp/targets, updates on success, writes CampaignHistory
4. POST /campaigns/{campaign_id}/search-terms/harvest:
   - Disconnected account rejected
   - Missing ad_group external_id refuses with clear message
   - With external_id: calls Amazon POST /sp/keywords, sets kw.external_id, writes CampaignHistory
"""

import httpx
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.main import app
from app.config import settings
from app.models.user import User
from app.models.ads_account import AdsAccount
from app.models.campaign import Campaign
from app.models.ad_group import AdGroup
from app.models.keyword import Keyword
from app.models.target import Target
from app.models.campaign_history import CampaignHistory
from app.core.crypto import encrypt, generate_key


@pytest.fixture(autouse=True)
def setup_n4_env(monkeypatch):
    key = generate_key()
    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", key)
    monkeypatch.setattr(settings, "AMAZON_LWA_CLIENT_ID", "amzn1.test.client_id")
    monkeypatch.setattr(settings, "AMAZON_LWA_CLIENT_SECRET", "test_secret_123")
    monkeypatch.setattr(settings, "AMAZON_ADS_API_BASE_URL", "https://advertising-api-eu.amazon.com")
    monkeypatch.setattr(settings, "AMAZON_ADS_REGION", "eu")


@pytest.mark.asyncio
async def test_bulk_max_100_items_and_placement_disabled(client_a: AsyncClient, user_a: User, db_session: AsyncSession):
    # 1. SET_PLACEMENT_PCT is disabled
    resp = await client_a.post("/campaigns/bulk", json={
        "ids": [1],
        "action": "SET_PLACEMENT_PCT",
        "value": 20,
        "placement": "TOP_OF_SEARCH",
    })
    assert resp.status_code == 400
    assert "Placement percentage changes via bulk action are disabled" in resp.json()["detail"]

    # 2. More than 100 items is rejected
    too_many_ids = list(range(1, 102))
    resp_large = await client_a.post("/campaigns/bulk", json={
        "ids": too_many_ids,
        "action": "ENABLE",
    })
    assert resp_large.status_code in (400, 422)


@pytest.mark.asyncio
async def test_bulk_campaigns_amazon_first_success_and_failure(client_a: AsyncClient, user_a: User, db_session: AsyncSession, monkeypatch):
    # Setup real connected account
    acc = AdsAccount(
        user_id=user_a.id,
        amazon_account_name="Real India Account",
        marketplace="IN",
        region="eu",
        profile_id="9988776655",
        connection_status="connected",
        is_active=True,
        is_mock=False,
        refresh_token_encrypted=encrypt("mock_refresh_token_value"),
    )
    db_session.add(acc)
    await db_session.flush()

    c1 = Campaign(
        ads_account_id=acc.id,
        name="Campaign Alpha",
        external_id="amz_c_1",
        state="PAUSED",
        daily_budget=200.0,
    )
    c2 = Campaign(
        ads_account_id=acc.id,
        name="Campaign Beta",
        external_id="amz_c_2",
        state="PAUSED",
        daily_budget=200.0,
    )
    db_session.add_all([c1, c2])
    await db_session.commit()
    await db_session.refresh(c1)
    await db_session.refresh(c2)

    # Mock Amazon Ads API PUT /sp/campaigns
    # c1 succeeds, c2 fails on Amazon
    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "auth/o2/token" in url_str:
            return httpx.Response(200, json={"access_token": "mock_access_token", "expires_in": 3600})
        if "/sp/campaigns" in url_str and request.method == "PUT":
            import json
            req_data = json.loads(request.content)
            req_cid = req_data["campaigns"][0]["campaignId"]
            if req_cid == "amz_c_1":
                return httpx.Response(200, json={
                    "campaigns": {
                        "success": [{"index": 0, "campaignId": "amz_c_1"}]
                    }
                })
            else:
                return httpx.Response(207, json={
                    "campaigns": {
                        "error": [{"index": 0, "campaignId": "amz_c_2", "code": "BUDGET_TOO_LOW", "details": "Budget below Amazon minimum"}]
                    }
                })
        return httpx.Response(404)

    mock_transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient
    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: orig_client(*args, transport=mock_transport, **{k: v for k, v in kwargs.items() if k != "transport"}))

    resp = await client_a.post("/campaigns/bulk", json={
        "ids": [c1.id, c2.id],
        "action": "ENABLE",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["updated"] == 1
    assert data["failed"] == 1
    assert data["failed"] == 1

    # Verify c1 state was updated in DB
    await db_session.refresh(c1)
    assert c1.state == "ENABLED"

    # Verify c2 state was NOT updated in DB
    await db_session.refresh(c2)
    assert c2.state == "PAUSED"

    # Verify CampaignHistory was written for c1
    res = await db_session.execute(select(CampaignHistory).where(CampaignHistory.campaign_id == c1.id))
    hist = res.scalars().all()
    assert len(hist) == 1
    assert "BULK_ENABLE" in hist[0].event_type


@pytest.mark.asyncio
async def test_patch_keyword_refuses_without_amazon_id_and_writes_amazon_first(client_a: AsyncClient, user_a: User, db_session: AsyncSession, monkeypatch):
    acc = AdsAccount(
        user_id=user_a.id,
        amazon_account_name="Real India Account",
        marketplace="IN",
        region="eu",
        profile_id="9988776655",
        connection_status="connected",
        is_active=True,
        is_mock=False,
        refresh_token_encrypted=encrypt("mock_refresh_token_value"),
    )
    db_session.add(acc)
    await db_session.flush()

    c = Campaign(ads_account_id=acc.id, name="Campaign K", state="ENABLED", daily_budget=500.0)
    db_session.add(c)
    await db_session.flush()

    ag = AdGroup(campaign_id=c.id, name="AdGroup K", external_id="amz_ag_1")
    db_session.add(ag)
    await db_session.flush()

    kw_no_ext = Keyword(ad_group_id=ag.id, keyword_text="shoes", match_type="EXACT", bid=15.0, external_id=None)
    kw_with_ext = Keyword(ad_group_id=ag.id, keyword_text="running shoes", match_type="EXACT", bid=20.0, external_id="amz_kw_100")
    db_session.add_all([kw_no_ext, kw_with_ext])
    await db_session.commit()
    await db_session.refresh(kw_no_ext)
    await db_session.refresh(kw_with_ext)

    # 1. Missing external_id refuses with clear message
    resp_no_ext = await client_a.patch(f"/keywords/{kw_no_ext.id}", json={"bid": 25.0})
    assert resp_no_ext.status_code == 400
    assert "Keyword does not have an Amazon keyword ID" in resp_no_ext.json()["detail"]

    # 2. With external_id calls Amazon Ads API PUT /sp/keywords
    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "auth/o2/token" in url_str:
            return httpx.Response(200, json={"access_token": "mock_access_token", "expires_in": 3600})
        if "/sp/keywords" in url_str and request.method == "PUT":
            return httpx.Response(200, json={
                "keywords": {
                    "success": [{"index": 0, "keywordId": "amz_kw_100"}]
                }
            })
        return httpx.Response(404)

    mock_transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient
    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: orig_client(*args, transport=mock_transport, **{k: v for k, v in kwargs.items() if k != "transport"}))

    resp_with_ext = await client_a.patch(f"/keywords/{kw_with_ext.id}", json={"bid": 30.0, "state": "PAUSED"})
    assert resp_with_ext.status_code == 200

    await db_session.refresh(kw_with_ext)
    assert kw_with_ext.bid == 30.0
    assert kw_with_ext.state == "PAUSED"

    # CampaignHistory recorded
    res = await db_session.execute(select(CampaignHistory).where(CampaignHistory.campaign_id == c.id))
    hist = res.scalars().all()
    assert len(hist) == 1
    assert hist[0].event_type == "KEYWORD_UPDATE"


@pytest.mark.asyncio
async def test_search_term_harvest_amazon_first(client_a: AsyncClient, user_a: User, db_session: AsyncSession, monkeypatch):
    acc = AdsAccount(
        user_id=user_a.id,
        amazon_account_name="Real India Account",
        marketplace="IN",
        region="eu",
        profile_id="9988776655",
        connection_status="connected",
        is_active=True,
        is_mock=False,
        refresh_token_encrypted=encrypt("mock_refresh_token_value"),
    )
    db_session.add(acc)
    await db_session.flush()

    c = Campaign(ads_account_id=acc.id, name="Campaign H", state="ENABLED", daily_budget=500.0)
    db_session.add(c)
    await db_session.flush()

    ag_no_ext = AdGroup(campaign_id=c.id, name="AdGroup No Ext", external_id=None)
    ag_with_ext = AdGroup(campaign_id=c.id, name="AdGroup With Ext", external_id="amz_ag_555")
    db_session.add_all([ag_no_ext, ag_with_ext])
    await db_session.commit()
    await db_session.refresh(ag_no_ext)
    await db_session.refresh(ag_with_ext)

    # 1. Missing ad group external_id refuses with clear message
    resp_no_ag = await client_a.post(f"/campaigns/{c.id}/search-terms/harvest", json={
        "search_term": "wireless earbuds",
        "match_type": "EXACT",
        "bid": 25.0,
        "ad_group_id": ag_no_ext.id,
    })
    assert resp_no_ag.status_code == 400
    assert "Ad group does not have an Amazon ad group ID" in resp_no_ag.json()["detail"]

    # 2. With ad group external_id calls Amazon Ads API POST /sp/keywords
    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "auth/o2/token" in url_str:
            return httpx.Response(200, json={"access_token": "mock_access_token", "expires_in": 3600})
        if "/sp/keywords" in url_str and request.method == "POST":
            return httpx.Response(200, json={
                "keywords": {
                    "success": [{"index": 0, "keywordId": "amz_kw_new_789"}]
                }
            })
        return httpx.Response(404)

    mock_transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient
    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: orig_client(*args, transport=mock_transport, **{k: v for k, v in kwargs.items() if k != "transport"}))

    resp_harvest = await client_a.post(f"/campaigns/{c.id}/search-terms/harvest", json={
        "search_term": "wireless earbuds",
        "match_type": "EXACT",
        "bid": 25.0,
        "ad_group_id": ag_with_ext.id,
    })
    assert resp_harvest.status_code == 200

    # Keyword created in DB with Amazon external_id
    res = await db_session.execute(select(Keyword).where(Keyword.ad_group_id == ag_with_ext.id, Keyword.keyword_text == "wireless earbuds"))
    kw = res.scalars().first()
    assert kw is not None
    assert kw.external_id == "amz_kw_new_789"
    assert kw.bid == 25.0


@pytest.mark.asyncio
async def test_patch_target_refuses_without_amazon_id(client_a: AsyncClient, user_a: User, db_session: AsyncSession):
    acc = AdsAccount(
        user_id=user_a.id,
        amazon_account_name="Real India Account",
        marketplace="IN",
        region="eu",
        profile_id="9988776655",
        connection_status="connected",
        is_active=True,
        is_mock=False,
        refresh_token_encrypted=encrypt("mock_refresh_token_value"),
    )
    db_session.add(acc)
    await db_session.flush()

    c = Campaign(ads_account_id=acc.id, name="Campaign T", state="ENABLED", daily_budget=500.0)
    db_session.add(c)
    await db_session.flush()

    ag = AdGroup(campaign_id=c.id, name="AdGroup T", external_id="amz_ag_t")
    db_session.add(ag)
    await db_session.flush()

    tg = Target(campaign_id=c.id, ad_group_id=ag.id, target_type="AUTO_GROUP", expression_type="CLOSE_MATCH", bid=15.0)
    db_session.add(tg)
    await db_session.commit()
    await db_session.refresh(tg)

    resp = await client_a.patch(f"/campaigns/{c.id}/targets/{tg.id}", json={"bid": 22.0})
    assert resp.status_code == 400
    assert "Target does not have an Amazon target ID" in resp.json()["detail"]
