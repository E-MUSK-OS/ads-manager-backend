import pytest
from httpx import AsyncClient
from datetime import date, timedelta
from sqlalchemy import select
from app.models.campaign import Campaign
from app.models.metric_daily import MetricDaily
from app.providers.ads.mock_provider import MockAdsProvider

@pytest.mark.asyncio
async def test_campaigns_hub_weighted_summary_and_sorting(client_a: AsyncClient, account_a, db_session):
    provider = MockAdsProvider(db_session)
    await provider.generate_for_new_connection(account_a.id)

    # 1. Test GET /campaigns with ads_account_id
    resp = await client_a.get(f"/campaigns?ads_account_id={account_a.id}")
    assert resp.status_code == 200
    data = resp.json()

    assert "items" in data
    assert "summary" in data
    assert "total_count" in data
    assert data["total_count"] == 10

    summary = data["summary"]
    current = summary["current"]

    # Calculate exact sum of spend and sales across all items
    total_spend = sum(item["spend"] for item in data["items"])
    total_sales = sum(item["sales"] for item in data["items"])

    assert current["spend"] == pytest.approx(total_spend, 0.01)
    assert current["sales"] == pytest.approx(total_sales, 0.01)

    expected_acos = round((total_spend / total_sales) * 100.0, 2)
    assert current["acos"] == expected_acos

    # Verify summary.current.acos is NOT the row average of ACOS
    row_acos_vals = [item["acos"] for item in data["items"] if item["acos"] is not None]
    if row_acos_vals:
        row_avg_acos = round(sum(row_acos_vals) / len(row_acos_vals), 2)
        assert current["acos"] != row_avg_acos

    # 2. Test Bleeding ACOS Sorting (sort_by=acos&sort_dir=desc)
    resp_sorted = await client_a.get(f"/campaigns?ads_account_id={account_a.id}&sort_by=acos&sort_dir=desc")
    assert resp_sorted.status_code == 200
    sorted_items = resp_sorted.json()["items"]

    # Top items must have acos_is_infinite == True (bleeding)
    first_item = sorted_items[0]
    assert first_item["acos_is_infinite"] is True
    assert first_item["spend"] > 0
    assert first_item["sales"] == 0.0

    # Ensure that all acos_is_infinite items appear before any finite numeric acos item
    found_finite = False
    for it in sorted_items:
        if not it["acos_is_infinite"] and it["acos"] is not None:
            found_finite = True
        if found_finite:
            assert it["acos_is_infinite"] is False

@pytest.mark.asyncio
async def test_campaign_meta_and_timeseries(client_a: AsyncClient, account_a, db_session):
    provider = MockAdsProvider(db_session)
    await provider.generate_for_new_connection(account_a.id)

    # 1. Meta endpoint
    resp = await client_a.get(f"/campaigns/meta?ads_account_id={account_a.id}")
    assert resp.status_code == 200
    meta = resp.json()
    assert meta["currency"]["code"] == "INR"
    assert meta["currency"]["symbol"] == "₹"
    assert meta["limits"]["min_daily_budget"] == 100.0
    assert "status_counts" in meta
    assert meta["status_counts"]["ALL"] == 10
    assert "portfolios" in meta

    # 2. Timeseries endpoint
    resp_ts = await client_a.get(f"/campaigns/timeseries?ads_account_id={account_a.id}&compare_previous=true")
    assert resp_ts.status_code == 200
    ts_data = resp_ts.json()
    assert "series" in ts_data
    assert "previous_series" in ts_data
    assert len(ts_data["series"]) >= 1

@pytest.mark.asyncio
async def test_patch_campaign_validations(client_a: AsyncClient, account_a, db_session):
    provider = MockAdsProvider(db_session)
    await provider.generate_for_new_connection(account_a.id)

    # Find an archived campaign and an enabled campaign
    camps = (await db_session.execute(
        select(Campaign).where(Campaign.ads_account_id == account_a.id)
    )).scalars().all()
    archived_camp = next(c for c in camps if c.state == "ARCHIVED")
    enabled_camp = next(c for c in camps if c.state == "ENABLED" and c.daily_budget <= 1500.0)

    # 1. Modifying ARCHIVED campaign returns 409
    resp_arch = await client_a.patch(f"/campaigns/{archived_camp.id}", json={"daily_budget": 500.0})
    assert resp_arch.status_code == 409
    assert "Archived campaigns cannot be modified" in resp_arch.text

    # 2. Setting daily_budget = 50 (< 100.0 min) returns 422
    resp_low_budget = await client_a.patch(f"/campaigns/{enabled_camp.id}", json={"daily_budget": 50.0})
    assert resp_low_budget.status_code == 422
    detail = resp_low_budget.json().get("detail", [])
    assert any(err.get("field") == "daily_budget" for err in detail)

    # 3. Valid update
    resp_valid = await client_a.patch(
        f"/campaigns/{enabled_camp.id}",
        json={"daily_budget": 1500.0, "placement_top_of_search_pct": 50}
    )
    assert resp_valid.status_code == 200
    assert resp_valid.json()["daily_budget"] == 1500.0

@pytest.mark.asyncio
async def test_bulk_actions_clamping_and_archived_skip(client_a: AsyncClient, account_a, db_session):
    provider = MockAdsProvider(db_session)
    await provider.generate_for_new_connection(account_a.id)

    camps = (await db_session.execute(
        select(Campaign).where(Campaign.ads_account_id == account_a.id)
    )).scalars().all()
    archived_camp = next(c for c in camps if c.state == "ARCHIVED")
    enabled_camp = next(c for c in camps if c.state == "ENABLED" and c.daily_budget <= 1500.0)

    # Test BUDGET_DECREASE_PCT = 95 on enabled campaign (e.g. 500 or 1000 budget - 95% -> < 100 -> clamps to 100.0)
    # Also include archived campaign to verify it is skipped
    resp = await client_a.post("/campaigns/bulk", json={
        "ads_account_id": account_a.id,
        "campaign_ids": [enabled_camp.id, archived_camp.id],
        "action": "BUDGET_DECREASE_PCT",
        "value": 95.0
    })
    assert resp.status_code == 200
    res_data = resp.json()

    assert res_data["updated"] >= 1
    assert res_data["skipped"] >= 1

    results_map = {r["id"]: r for r in res_data["results"]}
    assert results_map[enabled_camp.id]["status"] == "updated"
    assert results_map[enabled_camp.id]["daily_budget"] == 100.0  # Clamped to min 100.0

    assert results_map[archived_camp.id]["status"] == "skipped"
    assert "archived" in results_map[archived_camp.id]["reason"].lower()
