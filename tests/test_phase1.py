import pytest
from sqlalchemy import select, func
from app.core.marketplace_rules import compute_max_effective_bid, MARKETPLACE_RULES, marketplace_or_in
from app.providers.ads.mock_provider import MockAdsProvider
from app.models.campaign import Campaign
from app.models.metric_daily import MetricDaily
from app.models.placement_metric_daily import PlacementMetricDaily
from app.models.target import Target

def test_compute_max_effective_bid():
    val = compute_max_effective_bid(15.0, 100, "UP_AND_DOWN", "TOP_OF_SEARCH")
    assert val == 60.0

    # Down only / fixed
    assert compute_max_effective_bid(15.0, 100, "DOWN_ONLY", "TOP_OF_SEARCH") == 30.0
    assert compute_max_effective_bid(15.0, 50, "FIXED", "PRODUCT_PAGES") == 22.5

    # Rest of search with UP_AND_DOWN (1.5x)
    assert compute_max_effective_bid(10.0, 50, "UP_AND_DOWN", "REST_OF_SEARCH") == 22.5

def test_marketplace_rules_in():
    rules = marketplace_or_in("IN")
    assert rules["currency_code"] == "INR"
    assert rules["currency_symbol"] == "₹"
    assert rules["locale"] == "en-IN"
    assert rules["min_daily_budget"] == 100.0
    assert rules["max_daily_budget"] == 20000000.0
    assert rules["min_bid"] == 1.0
    assert rules["max_bid"] == 5000.0
    assert rules["default_bid"] == 15.0

@pytest.mark.asyncio
async def test_mock_provider_phase1_coherent_data(db_session, account_a):
    provider = MockAdsProvider(db_session)
    await provider.generate_for_new_connection(account_a.id)

    # 1. Verify 10 campaigns
    camps = (await db_session.execute(
        select(Campaign).where(Campaign.ads_account_id == account_a.id)
    )).scalars().all()
    assert len(camps) == 10

    types = {c.campaign_type for c in camps}
    assert "SPONSORED_PRODUCTS" in types
    assert "SPONSORED_BRANDS" in types
    assert "SPONSORED_DISPLAY" in types

    portfolios = {c.portfolio_name for c in camps}
    assert {"Bestsellers", "Festive Sale", "New Launches"}.issubset(portfolios)

    # States & Delivery
    states = {c.state for c in camps}
    assert "ENABLED" in states
    assert "PAUSED" in states
    assert "ARCHIVED" in states

    delivery_statuses = {c.delivery_status for c in camps}
    assert "OUT_OF_BUDGET" in delivery_statuses
    assert "DELIVERING" in delivery_statuses

    # 2. Verify PlacementMetricDaily exact reconciliation with metrics_daily
    m_rows = (await db_session.execute(
        select(MetricDaily).where(
            MetricDaily.entity_type == "campaign",
            MetricDaily.entity_id.in_([c.id for c in camps])
        )
    )).scalars().all()
    assert len(m_rows) > 0

    for m in m_rows:
        p_rows = (await db_session.execute(
            select(PlacementMetricDaily).where(
                PlacementMetricDaily.campaign_id == m.entity_id,
                PlacementMetricDaily.date == m.date
            )
        )).scalars().all()
        assert len(p_rows) == 3, f"Expected 3 placements for campaign {m.entity_id} on {m.date}, got {len(p_rows)}"

        sum_imp = sum(p.impressions for p in p_rows)
        sum_clk = sum(p.clicks for p in p_rows)
        sum_spd = round(sum(p.spend for p in p_rows), 2)
        sum_sls = round(sum(p.sales for p in p_rows), 2)
        sum_ord = sum(p.orders for p in p_rows)

        assert sum_imp == m.impressions, f"Impression mismatch: {sum_imp} vs {m.impressions}"
        assert sum_clk == m.clicks, f"Clicks mismatch: {sum_clk} vs {m.clicks}"
        assert sum_spd == round(m.spend, 2), f"Spend mismatch: {sum_spd} vs {m.spend}"
        assert sum_sls == round(m.sales, 2), f"Sales mismatch: {sum_sls} vs {m.sales}"
        assert sum_ord == m.orders, f"Orders mismatch: {sum_ord} vs {m.orders}"

