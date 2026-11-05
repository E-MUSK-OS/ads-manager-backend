import pytest
from httpx import AsyncClient
from datetime import date, timedelta
from sqlalchemy import select, func
from app.models.campaign import Campaign
from app.models.campaign_draft import CampaignDraft
from app.models.ad_group import AdGroup
from app.models.keyword import Keyword
from app.models.negative_keyword import NegativeKeyword
from app.models.target import Target
from app.models.placement_metric_daily import PlacementMetricDaily
from app.models.metric_daily import MetricDaily
from app.providers.ads.mock_provider import MockAdsProvider
from app.core.marketplace_rules import compute_max_effective_bid


@pytest.mark.asyncio
async def test_campaign_placements_reconciliation(client_a: AsyncClient, account_a, db_session):
    provider = MockAdsProvider(db_session)
    await provider.generate_for_new_connection(account_a.id)

    # Pick an active manual campaign
    camp = (await db_session.execute(
        select(Campaign).where(Campaign.ads_account_id == account_a.id, Campaign.state == "ENABLED")
    )).scalars().first()
    assert camp is not None

    resp = await client_a.get(f"/campaigns/{camp.id}/placements")
    assert resp.status_code == 200
    placements = resp.json()

    assert len(placements) == 3
    placement_names = [p["placement"] for p in placements]
    assert "TOP_OF_SEARCH" in placement_names
    assert "REST_OF_SEARCH" in placement_names
    assert "PRODUCT_PAGES" in placement_names

    # Check that each row has max_effective_bid
    for p in placements:
        assert "max_effective_bid" in p
        expected_max = compute_max_effective_bid(p["base_bid"], p["modifier_pct"], camp.bidding_strategy, p["placement"])
        assert p["max_effective_bid"] == expected_max

    # Check metrics sum matches MetricDaily sum
    tot_imp = sum(p["impressions"] for p in placements)
    tot_clk = sum(p["clicks"] for p in placements)
    tot_spd = round(sum(p["spend"] for p in placements), 2)
    tot_sls = round(sum(p["sales"] for p in placements), 2)
    tot_ord = sum(p["orders"] for p in placements)

    # Compare with campaign's MetricDaily for the same 7-day window
    today = date.today()
    start_date = today - timedelta(days=6)
    m_res = (await db_session.execute(
        select(
            func.coalesce(func.sum(MetricDaily.impressions), 0),
            func.coalesce(func.sum(MetricDaily.clicks), 0),
            func.coalesce(func.sum(MetricDaily.spend), 0.0),
            func.coalesce(func.sum(MetricDaily.sales), 0.0),
            func.coalesce(func.sum(MetricDaily.orders), 0)
        ).where(
            MetricDaily.entity_type == "campaign",
            MetricDaily.entity_id == camp.id,
            MetricDaily.date >= start_date,
            MetricDaily.date <= today
        )
    )).first()

    assert tot_imp == int(m_res[0])
    assert tot_clk == int(m_res[1])
    assert tot_spd == pytest.approx(float(m_res[2]), 0.01)
    assert tot_sls == pytest.approx(float(m_res[3]), 0.01)
    assert tot_ord == int(m_res[4])


@pytest.mark.asyncio
async def test_patch_placements_and_archived_guard(client_a: AsyncClient, account_a, db_session):
    provider = MockAdsProvider(db_session)
    await provider.generate_for_new_connection(account_a.id)

    # Active campaign
    camp = (await db_session.execute(
        select(Campaign).where(Campaign.ads_account_id == account_a.id, Campaign.state == "ENABLED")
    )).scalars().first()

    # Patch valid placements
    patch_resp = await client_a.patch(f"/campaigns/{camp.id}/placements", json={
        "top_of_search_pct": 75,
        "product_pages_pct": 25,
        "rest_of_search_pct": 10
    })
    assert patch_resp.status_code == 200
    data = patch_resp.json()
    top_p = next(p for p in data if p["placement"] == "TOP_OF_SEARCH")
    assert top_p["modifier_pct"] == 75

    # Invalid modifier (> 900)
    bad_resp = await client_a.patch(f"/campaigns/{camp.id}/placements", json={
        "top_of_search_pct": 950
    })
    assert bad_resp.status_code == 422

    # Archived campaign guard
    arch_camp = (await db_session.execute(
        select(Campaign).where(Campaign.ads_account_id == account_a.id, Campaign.state == "ARCHIVED")
    )).scalars().first()
    if arch_camp:
        arch_resp = await client_a.patch(f"/campaigns/{arch_camp.id}/placements", json={
            "top_of_search_pct": 50
        })
        assert arch_resp.status_code == 409


@pytest.mark.asyncio
async def test_search_term_harvest_and_negate_idempotency(client_a: AsyncClient, account_a, db_session):
    provider = MockAdsProvider(db_session)
    await provider.generate_for_new_connection(account_a.id)

    camp = (await db_session.execute(
        select(Campaign).where(Campaign.ads_account_id == account_a.id, Campaign.state == "ENABLED")
    )).scalars().first()
    assert camp is not None

    # 1. Harvest search term
    harvest_resp = await client_a.post(f"/campaigns/{camp.id}/search-terms/harvest", json={
        "search_term": "wireless running earphones",
        "match_type": "EXACT",
        "bid": 25.50
    })
    assert harvest_resp.status_code == 200
    h_data = harvest_resp.json()
    assert h_data["status"] == "ok"
    assert h_data["keyword_text"] == "wireless running earphones"
    assert h_data["match_type"] == "EXACT"
    assert h_data["bid"] == 25.50

    # Harvest again idempotently with updated bid
    harvest_resp2 = await client_a.post(f"/campaigns/{camp.id}/search-terms/harvest", json={
        "search_term": "wireless running earphones",
        "match_type": "EXACT",
        "bid": 30.00
    })
    assert harvest_resp2.status_code == 200
    assert harvest_resp2.json()["bid"] == 30.00

    # 2. Conflict Guard: Attempt to negate this exact text as NEGATIVE_EXACT
    conflict_resp = await client_a.post(f"/campaigns/{camp.id}/search-terms/negate", json={
        "search_term": "wireless running earphones",
        "match_type": "NEGATIVE_EXACT"
    })
    assert conflict_resp.status_code == 422
    assert "conflicts" in conflict_resp.json()["detail"][0]["message"].lower()

    # Negate another non-conflicting search term
    neg_resp = await client_a.post(f"/campaigns/{camp.id}/search-terms/negate", json={
        "search_term": "free running earphones fake",
        "match_type": "NEGATIVE_EXACT"
    })
    assert neg_resp.status_code == 200
    n_data = neg_resp.json()
    assert n_data["status"] == "ok"
    assert n_data["keyword_text"] == "free running earphones fake"

    # Negate again idempotently
    neg_resp2 = await client_a.post(f"/campaigns/{camp.id}/search-terms/negate", json={
        "search_term": "free running earphones fake",
        "match_type": "NEGATIVE_EXACT"
    })
    assert neg_resp2.status_code == 200


@pytest.mark.asyncio
async def test_atomic_wizard_launch_and_draft_deletion(client_a: AsyncClient, account_a, user_a, db_session):
    # Create a draft first
    draft = CampaignDraft(
        user_id=user_a.id,
        ads_account_id=account_a.id,
        name="Festive Diwali Launch Draft",
        step=5,
        payload={"notes": "draft to be launched"}
    )
    db_session.add(draft)
    await db_session.commit()
    await db_session.refresh(draft)
    draft_id = draft.id

    # Wizard payload
    wizard_payload = {
        "ads_account_id": account_a.id,
        "draft_id": draft_id,
        "name": "Festive Diwali Launch Campaign",
        "campaign_type": "SPONSORED_PRODUCTS",
        "targeting_type": "MANUAL",
        "state": "ENABLED",
        "start_date": str(date.today()),
        "daily_budget": 1500.0,
        "target_acos": 22.0,
        "bidding_strategy": "UP_AND_DOWN",
        "placement_top_of_search_pct": 50,
        "placement_product_pages_pct": 20,
        "placement_rest_of_search_pct": 0,
        "portfolio_name": "Festive Sale",
        "product_ids": [1],
        "ad_groups": [
            {
                "name": "Diwali Ad Group 1",
                "default_bid": 18.0,
                "keywords": [
                    {"keyword_text": "diwali gift box", "match_type": "EXACT", "bid": 22.0},
                    {"keyword_text": "festive dry fruits", "match_type": "BROAD", "bid": 18.0}
                ],
                "targets": []
            }
        ],
        "negative_keywords": [
            {"keyword_text": "cheap diwali gift box", "match_type": "NEGATIVE_EXACT"}
        ],
        "negative_asins": [],
        "negative_brands": ["GenericBrand"]
    }

    launch_resp = await client_a.post("/campaigns/wizard", json=wizard_payload)
    assert launch_resp.status_code == 200
    res = launch_resp.json()
    new_camp_id = res["id"]
    assert res["name"] == "Festive Diwali Launch Campaign"
    assert res["state"] == "ENABLED"

    # Verify CampaignDraft is deleted atomically
    deleted_draft = (await db_session.execute(
        select(CampaignDraft).where(CampaignDraft.id == draft_id)
    )).scalars().first()
    assert deleted_draft is None

    # Verify campaign entities created in DB
    created_camp = (await db_session.execute(
        select(Campaign).where(Campaign.id == new_camp_id)
    )).scalars().first()
    assert created_camp is not None
    assert created_camp.daily_budget == 1500.0
    assert created_camp.placement_top_of_search_pct == 50
    assert created_camp.portfolio_name == "Festive Sale"

    # Check conflict check in wizard payload:
    # A wizard payload where a positive EXACT keyword matches a NEGATIVE_EXACT keyword must return 422
    conflict_payload = dict(wizard_payload)
    conflict_payload["name"] = "Conflicting Campaign"
    conflict_payload["draft_id"] = None
    conflict_payload["negative_keywords"] = [
        {"keyword_text": "diwali gift box", "match_type": "NEGATIVE_EXACT"}
    ]
    bad_launch = await client_a.post("/campaigns/wizard", json=conflict_payload)
    assert bad_launch.status_code == 422
    assert "conflicts" in bad_launch.json()["detail"][0]["message"].lower()

    # Check history endpoint for the newly created campaign
    hist_resp = await client_a.get(f"/campaigns/{new_camp_id}/history")
    assert hist_resp.status_code == 200
    hist_events = hist_resp.json()
    assert len(hist_events) >= 1
    assert any(ev["event_type"] == "CAMPAIGN_LAUNCHED" for ev in hist_events)
