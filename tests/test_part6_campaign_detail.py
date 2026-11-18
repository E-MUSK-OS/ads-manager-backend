import pytest
from httpx import AsyncClient, ASGITransport
from datetime import date, timedelta
from app.main import app
from app.database import get_db
from app.dependencies import get_current_user
from app.models.campaign import Campaign
from app.models.ad_group import AdGroup
from app.models.metric_daily import MetricDaily
from app.models.placement_metric_daily import PlacementMetricDaily
from app.models.campaign_history import CampaignHistory
from sqlalchemy import select

@pytest.mark.asyncio
async def test_campaign_detail_endpoints(db_session, user_a, account_a):
    c = Campaign(
        ads_account_id=account_a.id,
        name="Campaign Detail Test",
        state="ENABLED",
        delivery_status="DELIVERING",
        daily_budget=500.0,
        targeting_type="MANUAL",
        campaign_type="SPONSORED_PRODUCTS",
        site="AMAZON_AND_BEYOND",
        bidding_strategy="DOWN_ONLY",
        start_date=date.today() - timedelta(days=10),
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)

    ag = AdGroup(
        campaign_id=c.id,
        name="Ad Group 1",
        state="ENABLED",
        default_bid=15.0
    )
    db_session.add(ag)

    m = MetricDaily(
        entity_type="campaign",
        entity_id=c.id,
        date=date.today() - timedelta(days=1),
        impressions=1000,
        clicks=50,
        spend=500.0,
        sales=2500.0,
        orders=10,
    )
    db_session.add(m)

    plc = PlacementMetricDaily(
        campaign_id=c.id,
        placement="TOP_OF_SEARCH",
        date=date.today() - timedelta(days=1),
        impressions=400,
        clicks=25,
        spend=300.0,
        sales=1800.0,
        orders=8,
    )
    db_session.add(plc)
    await db_session.commit()

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: user_a

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. GET /campaigns/{id}
        res = await client.get(f"/campaigns/{c.id}")
        assert res.status_code == 200
        data = res.json()
        assert data["id"] == c.id
        assert data["name"] == "Campaign Detail Test"
        assert data["bidding_strategy"] == "DOWN_ONLY"
        assert float(data["daily_budget"]) == 500.0

        # 2. GET /campaigns/summary?campaign_id={id}
        s_res = await client.get(f"/campaigns/summary?campaign_id={c.id}&granularity=daily")
        assert s_res.status_code == 200
        s_data = s_res.json()
        assert "series" in s_data
        assert "totals" in s_data
        assert s_data["totals"]["clicks"] == 50
        assert s_data["totals"]["spend"] == 500.0

        # 3. GET /campaigns/{id}/placements (Read-only)
        plc_res = await client.get(f"/campaigns/{c.id}/placements")
        assert plc_res.status_code == 200
        plc_data = plc_res.json()
        assert len(plc_data) == 3
        top_plc = next(p for p in plc_data if p["placement"] == "TOP_OF_SEARCH")
        assert top_plc["clicks"] == 25
        assert top_plc["spend"] == 300.0
        assert "max_effective_bid" in top_plc

        # 4. GET /campaigns/{id}/ad-groups
        ag_res = await client.get(f"/campaigns/{c.id}/ad-groups")
        assert ag_res.status_code == 200
        ag_data = ag_res.json()
        assert len(ag_data) == 1
        assert ag_data[0]["name"] == "Ad Group 1"

        # 5. PATCH /campaigns/{id} (Update name, daily_budget, bidding_strategy)
        new_name = "Phase6 Updated Campaign Name"
        new_budget = 750.0
        patch_res = await client.patch(
            f"/campaigns/{c.id}",
            json={
                "name": new_name,
                "daily_budget": new_budget,
                "bidding_strategy": "UP_AND_DOWN"
            }
        )
        assert patch_res.status_code == 200
        patched_data = patch_res.json()
        assert patched_data["name"] == new_name
        assert float(patched_data["daily_budget"]) == new_budget
        assert patched_data["bidding_strategy"] == "UP_AND_DOWN"

        # 6. Verify CampaignHistory was recorded
        hist_res = await client.get(f"/campaigns/{c.id}/history")
        assert hist_res.status_code == 200
        hist_data = hist_res.json()
        event_types = {h["event_type"] for h in hist_data}
        assert "NAME_UPDATE" in event_types
        assert "BUDGET_UPDATE" in event_types
        assert "STRATEGY_UPDATE" in event_types

        # 7. PUT /campaigns/{id}/state (Update state)
        state_res = await client.put(
            f"/campaigns/{c.id}/state",
            json={"state": "PAUSED"}
        )
        assert state_res.status_code == 200
        assert state_res.json()["state"] == "PAUSED"

        # Verify STATE_CHANGE in history
        hist_res2 = await client.get(f"/campaigns/{c.id}/history")
        assert hist_res2.status_code == 200
        event_types2 = {h["event_type"] for h in hist_res2.json()}
        assert "STATE_CHANGE" in event_types2

    app.dependency_overrides.clear()
