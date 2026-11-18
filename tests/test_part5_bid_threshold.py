import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import get_db
from app.dependencies import get_current_user
from app.models.campaign import Campaign
from app.models.campaign_bid_threshold import CampaignBidThreshold
from app.models.campaign_history import CampaignHistory
from sqlalchemy import select


@pytest.mark.asyncio
async def test_get_bid_threshold_none_initially(db_session, user_a, account_a):
    c = Campaign(
        ads_account_id=account_a.id,
        name="Campaign A1",
        state="ENABLED",
        delivery_status="DELIVERING",
        daily_budget=500.0,
        targeting_type="MANUAL",
        campaign_type="SPONSORED_PRODUCTS",
        site="AMAZON_AND_BEYOND",
        bidding_strategy="DOWN_ONLY",
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: user_a

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/campaigns/{c.id}/bid-threshold")
        assert resp.status_code == 200
        assert resp.json() is None


@pytest.mark.asyncio
async def test_put_bid_threshold_valid(db_session, user_a, account_a):
    c = Campaign(
        ads_account_id=account_a.id,
        name="Campaign A2",
        state="ENABLED",
        delivery_status="DELIVERING",
        daily_budget=500.0,
        targeting_type="MANUAL",
        campaign_type="SPONSORED_PRODUCTS",
        site="AMAZON_AND_BEYOND",
        bidding_strategy="DOWN_ONLY",
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: user_a

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        payload = {
            "min_bid": 10.5,
            "max_bid": 45.0,
            "target_acos": 25.0,
        }
        resp = await client.put(f"/campaigns/{c.id}/bid-threshold", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["campaign_id"] == c.id
        assert data["min_bid"] == 10.5
        assert data["max_bid"] == 45.0
        assert data["target_acos"] == 25.0

        # Check history entry was written
        hist_stmt = select(CampaignHistory).where(
            CampaignHistory.campaign_id == c.id,
            CampaignHistory.event_type == "BID_THRESHOLD_UPDATE",
        )
        hist = (await db_session.execute(hist_stmt)).scalars().first()
        assert hist is not None
        assert "Min: ₹10.50, Max: ₹45.00, Target ACOS: 25.0%" in hist.description


@pytest.mark.asyncio
async def test_put_bid_threshold_validation_errors(db_session, user_a, account_a):
    c = Campaign(
        ads_account_id=account_a.id,
        name="Campaign A3",
        state="ENABLED",
        delivery_status="DELIVERING",
        daily_budget=500.0,
        targeting_type="MANUAL",
        campaign_type="SPONSORED_PRODUCTS",
        site="AMAZON_AND_BEYOND",
        bidding_strategy="DOWN_ONLY",
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: user_a

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Min > Max
        resp = await client.put(f"/campaigns/{c.id}/bid-threshold", json={
            "min_bid": 50.0,
            "max_bid": 20.0,
            "target_acos": 20.0,
        })
        assert resp.status_code == 422

        # Negative bid
        resp = await client.put(f"/campaigns/{c.id}/bid-threshold", json={
            "min_bid": -5.0,
            "max_bid": 20.0,
        })
        assert resp.status_code == 422

        # Target ACOS > 100
        resp = await client.put(f"/campaigns/{c.id}/bid-threshold", json={
            "min_bid": 10.0,
            "max_bid": 20.0,
            "target_acos": 150.0,
        })
        assert resp.status_code == 422

        # Target ACOS < 0
        resp = await client.put(f"/campaigns/{c.id}/bid-threshold", json={
            "target_acos": -10.0,
        })
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_delete_bid_threshold(db_session, user_a, account_a):
    c = Campaign(
        ads_account_id=account_a.id,
        name="Campaign A4",
        state="ENABLED",
        delivery_status="DELIVERING",
        daily_budget=500.0,
        targeting_type="MANUAL",
        campaign_type="SPONSORED_PRODUCTS",
        site="AMAZON_AND_BEYOND",
        bidding_strategy="DOWN_ONLY",
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)

    bt = CampaignBidThreshold(
        campaign_id=c.id,
        min_bid=12.0,
        max_bid=30.0,
        target_acos=18.0,
        updated_by=user_a.id,
    )
    db_session.add(bt)
    await db_session.commit()

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: user_a

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Delete
        resp = await client.delete(f"/campaigns/{c.id}/bid-threshold")
        assert resp.status_code == 204

        # Verify it's gone
        resp_get = await client.get(f"/campaigns/{c.id}/bid-threshold")
        assert resp_get.status_code == 200
        assert resp_get.json() is None


@pytest.mark.asyncio
async def test_bid_threshold_user_isolation(db_session, user_a, user_b, account_a):
    c = Campaign(
        ads_account_id=account_a.id,
        name="Campaign A5",
        state="ENABLED",
        delivery_status="DELIVERING",
        daily_budget=500.0,
        targeting_type="MANUAL",
        campaign_type="SPONSORED_PRODUCTS",
        site="AMAZON_AND_BEYOND",
        bidding_strategy="DOWN_ONLY",
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)

    # User B tries to access Campaign A
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: user_b

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/campaigns/{c.id}/bid-threshold")
        assert resp.status_code == 404

        resp = await client.put(f"/campaigns/{c.id}/bid-threshold", json={"min_bid": 5.0})
        assert resp.status_code == 404

        resp = await client.delete(f"/campaigns/{c.id}/bid-threshold")
        assert resp.status_code == 404
