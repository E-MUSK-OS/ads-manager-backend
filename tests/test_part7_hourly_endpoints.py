import pytest
from httpx import AsyncClient, ASGITransport
from datetime import date, timedelta
from app.main import app
from app.database import get_db
from app.dependencies import get_current_user
from app.models.campaign import Campaign
from app.models.campaign_hourly_metric import CampaignHourlyMetric
from app.models.hourly_stream_status import HourlyStreamStatus


@pytest.mark.asyncio
async def test_hourly_endpoints_unconnected_state(db_session, user_a, account_a):
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: user_a

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Status endpoint when not connected
        resp = await client.get(f"/campaigns/hourly/status?ads_account_id={account_a.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_connected"] is False
        assert data["status"] == "NOT_CONNECTED"

        # 2. Summary endpoint when empty
        today = date.today()
        start = today - timedelta(days=7)
        resp2 = await client.get(
            f"/campaigns/hourly/summary?ads_account_id={account_a.id}&start={start}&end={today}"
        )
        assert resp2.status_code == 200
        s_data = resp2.json()
        assert s_data["is_connected"] is False
        assert s_data["has_data"] is False
        # Honest empty state: None values, never fake zeros
        assert s_data["totals"]["spend"] is None
        assert s_data["totals"]["sales"] is None

        # 3. Trends endpoint
        resp3 = await client.get(
            f"/campaigns/hourly/trends?ads_account_id={account_a.id}&start={start}&end={today}"
        )
        assert resp3.status_code == 200
        t_data = resp3.json()
        assert len(t_data["series"]) == 24
        assert t_data["series"][0]["label"] == "00:00"
        assert t_data["series"][23]["label"] == "23:00"

        # 4. Matrix endpoint
        resp4 = await client.get(
            f"/campaigns/hourly/matrix?ads_account_id={account_a.id}&start={start}&end={today}&metric=sales"
        )
        assert resp4.status_code == 200
        m_data = resp4.json()
        assert len(m_data["dates"]) == 8
        assert "summary_row" in m_data

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_hourly_endpoints_with_data_and_tenant_isolation(db_session, user_a, account_a, user_b, account_b):
    # Seed a campaign and hourly metrics for user_a
    c = Campaign(
        ads_account_id=account_a.id,
        amazon_campaign_id="amzn_h_1",
        name="Hourly Test Campaign",
        state="ENABLED",
        delivery_status="DELIVERING",
        daily_budget=500.0,
        targeting_type="MANUAL",
        campaign_type="SPONSORED_PRODUCTS",
        site="AMAZON_AND_BEYOND",
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)

    test_date = date.today() - timedelta(days=1)
    hm1 = CampaignHourlyMetric(
        ads_account_id=account_a.id,
        campaign_id=c.id,
        date=test_date,
        hour=14,  # 14:00 (Peak hour)
        impressions=1000,
        clicks=50,
        cost=250.0,
        purchases=5,
        sales=1500.0,
    )
    hm2 = CampaignHourlyMetric(
        ads_account_id=account_a.id,
        campaign_id=c.id,
        date=test_date,
        hour=3,  # 03:00 (Low hour)
        impressions=100,
        clicks=2,
        cost=10.0,
        purchases=0,
        sales=0.0,
    )
    st = HourlyStreamStatus(
        ads_account_id=account_a.id,
        status="ACTIVE",
        message_count=10,
    )
    db_session.add_all([hm1, hm2, st])
    await db_session.commit()

    # User A query
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: user_a

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Status is active
        res_st = await client.get(f"/campaigns/hourly/status?ads_account_id={account_a.id}")
        assert res_st.status_code == 200
        assert res_st.json()["is_connected"] is True
        assert res_st.json()["status"] == "ACTIVE"

        # Summary has real metrics
        res_sum = await client.get(
            f"/campaigns/hourly/summary?ads_account_id={account_a.id}&campaign_id={c.id}&start={test_date}&end={test_date}"
        )
        assert res_sum.status_code == 200
        sum_data = res_sum.json()
        assert sum_data["has_data"] is True
        assert sum_data["totals"]["impressions"] == 1100
        assert sum_data["totals"]["clicks"] == 52
        assert sum_data["totals"]["spend"] == 260.0
        assert sum_data["totals"]["orders"] == 5
        assert sum_data["totals"]["sales"] == 1500.0

        # Trends returns peak hour 14
        res_trends = await client.get(
            f"/campaigns/hourly/trends?ads_account_id={account_a.id}&campaign_id={c.id}&start={test_date}&end={test_date}"
        )
        assert res_trends.status_code == 200
        tr_data = res_trends.json()
        assert tr_data["has_data"] is True
        peaks = tr_data["peaks_and_lows"]["sales"]
        assert peaks["peak_hour"] == 14
        assert peaks["peak_value"] == 1500.0
        assert peaks["low_value"] == 0.0

        # Matrix has real values
        res_matrix = await client.get(
            f"/campaigns/hourly/matrix?ads_account_id={account_a.id}&campaign_id={c.id}&start={test_date}&end={test_date}&metric=sales"
        )
        assert res_matrix.status_code == 200
        mat_data = res_matrix.json()
        assert mat_data["has_data"] is True
        row = mat_data["rows"][0]
        assert row["hours"]["14"] == 1500.0
        assert row["hours"]["3"] == 0.0
        assert row["daily_total"] == 1500.0

    # User B should be blocked from accessing User A's hourly data (Tenant Isolation)
    app.dependency_overrides[get_current_user] = lambda: user_b

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Accessing account A with user B returns 404/403
        unauth_res = await client.get(f"/campaigns/hourly/status?ads_account_id={account_a.id}")
        assert unauth_res.status_code in (403, 404)

        unauth_res2 = await client.get(
            f"/campaigns/hourly/summary?ads_account_id={account_a.id}&start={test_date}&end={test_date}"
        )
        assert unauth_res2.status_code in (403, 404)

    app.dependency_overrides.clear()
