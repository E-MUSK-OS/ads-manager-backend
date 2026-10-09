import pytest
from httpx import AsyncClient
from app.core.marketplace_rules import today_for
from datetime import timedelta
from sqlalchemy import select
from app.models.campaign import Campaign
from app.models.ad_group import AdGroup
from app.models.keyword import Keyword
from app.models.campaign_product import CampaignProduct
from app.models.negative_keyword import NegativeKeyword
from app.models.metric_daily import MetricDaily

@pytest.mark.asyncio
async def test_create_campaign_and_ownership(client_a: AsyncClient, client_b: AsyncClient, account_a, account_b, db_session):
    # Test valid create (User A)
    today = today_for("IN")
    payload = {
        "ads_account_id": account_a.id,
        "name": "Test Campaign",
        "targeting_type": "MANUAL",
        "start_date": today.isoformat(),
        "daily_budget": 500.0,
        "product_ids": [1], # ID from fixture
        "ad_groups": [{
            "name": "AG 1",
            "default_bid": 10.0,
            "keywords": [{"keyword_text": "test", "match_type": "EXACT"}]
        }],
        "negative_keywords": [{"keyword_text": "cheap", "match_type": "NEGATIVE_EXACT"}]
    }
    
    resp = await client_a.post("/campaigns", json=payload)
    assert resp.status_code == 200
    c_id = resp.json()["id"]
    
    # Check rows created
    c = (await db_session.execute(select(Campaign))).scalars().first()
    assert c.name == "Test Campaign"
    assert (await db_session.execute(select(AdGroup))).scalars().first() is not None
    assert (await db_session.execute(select(Keyword))).scalars().first() is not None
    assert (await db_session.execute(select(CampaignProduct))).scalars().first() is not None
    assert (await db_session.execute(select(NegativeKeyword))).scalars().first() is not None
    
    # Test ownership: User B cannot read
    resp = await client_b.get(f"/campaigns/{c_id}")
    assert resp.status_code == 404
    
    # User B cannot update state
    resp = await client_b.put(f"/campaigns/{c_id}/state", json={"state": "PAUSED"})
    assert resp.status_code == 404
    
    # User B cannot duplicate
    resp = await client_b.post(f"/campaigns/{c_id}/duplicate")
    assert resp.status_code == 404
    
    # User B cannot create in User A's account
    resp = await client_b.post("/campaigns", json=payload)
    assert resp.status_code == 404

@pytest.mark.asyncio
async def test_create_validation_errors(client_a: AsyncClient, account_a):
    today = today_for("IN")
    payload = {
        "ads_account_id": account_a.id,
        "name": "Invalid Budget",
        "targeting_type": "MANUAL",
        "start_date": today.isoformat(),
        "daily_budget": 10.0, # Below IN minimum 50
        "product_ids": [1],
        "ad_groups": [{"name": "AG 1", "default_bid": 10.0, "keywords": [{"keyword_text": "test"}]}]
    }
    resp = await client_a.post("/campaigns", json=payload)
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["field"] == "daily_budget"
    
    # End before start
    payload["daily_budget"] = 500.0
    payload["end_date"] = (today - timedelta(days=1)).isoformat()
    resp = await client_a.post("/campaigns", json=payload)
    assert resp.status_code == 422

    # Start date yesterday
    payload["end_date"] = None
    payload["start_date"] = (today - timedelta(days=1)).isoformat()
    resp = await client_a.post("/campaigns", json=payload)
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["field"] == "start_date"

@pytest.mark.asyncio
async def test_bulk_archive_rule(client_a: AsyncClient, account_a, db_session):
    today = today_for("IN")
    # Create campaign
    c = Campaign(ads_account_id=account_a.id, name="C1", targeting_type="AUTO", state="ARCHIVED", start_date=today)
    db_session.add(c)
    await db_session.commit()
    
    # Try to unarchive
    resp = await client_a.post("/campaigns/bulk", json={"ids": [c.id], "action": "ENABLE"})
    assert resp.status_code == 200
    res = resp.json()["results"][0]
    assert res["ok"] is False
    assert "Cannot enable archived" in res["error"]

@pytest.mark.asyncio
async def test_report_and_summary(client_a: AsyncClient, account_a, db_session):
    today = today_for("IN")
    c1 = Campaign(ads_account_id=account_a.id, name="C1", targeting_type="AUTO", state="ENABLED", start_date=today-timedelta(days=10))
    c2 = Campaign(ads_account_id=account_a.id, name="C2", targeting_type="MANUAL", state="ENABLED", start_date=today-timedelta(days=10))
    db_session.add_all([c1, c2])
    await db_session.commit()
    
    # Add metrics
    m1 = MetricDaily(entity_type="campaign", entity_id=c1.id, date=today-timedelta(days=1), impressions=1000, clicks=10, spend=100.0, sales=500.0, orders=2)
    m2 = MetricDaily(entity_type="campaign", entity_id=c2.id, date=today-timedelta(days=1), impressions=2000, clicks=20, spend=300.0, sales=0.0, orders=0)
    db_session.add_all([m1, m2])
    await db_session.commit()
    
    # Report totals
    resp = await client_a.get(f"/campaigns/report?ads_account_id={account_a.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["totals"]["spend"] == 400.0
    assert data["totals"]["sales"] == 500.0
    
    # C2 has spend but no sales
    c2_item = next(item for item in data["items"] if item["id"] == c2.id)
    assert c2_item["acos"] is None
    assert c2_item["health"] == "BUDGET_LEAK"
    
    # Alerts
    resp = await client_a.get(f"/campaigns/alerts?ads_account_id={account_a.id}")
    assert resp.status_code == 200
    alerts = resp.json()
    assert len(alerts) == 1
    assert alerts[0]["id"] == c2.id
    
    # Summary
    resp = await client_a.get(f"/campaigns/summary?ads_account_id={account_a.id}&granularity=day")
    assert resp.status_code == 200
    summary = resp.json()
    assert summary["totals"]["spend"] == 400.0
    
@pytest.mark.asyncio
async def test_get_campaigns_array(client_a: AsyncClient, account_a, db_session):
    today = today_for("IN")
    c = Campaign(ads_account_id=account_a.id, name="C1", targeting_type="AUTO", state="ENABLED", start_date=today)
    db_session.add(c)
    await db_session.commit()
    
    resp = await client_a.get("/campaigns")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
    assert len(resp.json()) == 1
    
@pytest.mark.asyncio
async def test_marketplace_rules_endpoint(client_a: AsyncClient, account_a):
    resp = await client_a.get(f"/ads-accounts/{account_a.id}/rules")
    assert resp.status_code == 200
    assert resp.json()["currency"] == "INR"
    assert resp.json()["min_budget"] == 50.0

@pytest.mark.asyncio
async def test_report_regression_no_settings_empty(client_a: AsyncClient, account_a, db_session):
    # (a) Brand new user with no settings row
    # (b) Account with no campaigns/metrics (empty items, zero totals)
    resp = await client_a.get(f"/campaigns/report?ads_account_id={account_a.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 0
    assert data["totals"]["spend"] == 0.0

@pytest.mark.asyncio
async def test_report_unknown_marketplace(client_b: AsyncClient, account_b, db_session):
    # (c) account whose marketplace is not in MARKETPLACE_RULES
    account_b.marketplace = "FR"
    db_session.add(account_b)
    await db_session.commit()
    
    resp = await client_b.get(f"/campaigns/report?ads_account_id={account_b.id}")
    assert resp.status_code == 200
