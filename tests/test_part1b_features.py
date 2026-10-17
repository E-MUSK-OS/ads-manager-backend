import pytest
from httpx import AsyncClient
from datetime import timedelta
from sqlalchemy import select
from app.core.marketplace_rules import today_for
from app.models.campaign import Campaign
from app.models.ad_group import AdGroup
from app.models.keyword import Keyword
from app.models.target import Target
from app.models.product import Product
from app.models.negative_keyword import NegativeKeyword
from app.models.search_term import SearchTerm
from app.models.metric_daily import MetricDaily
from app.models.user_campaign_settings import UserCampaignSettings

@pytest.mark.asyncio
async def test_wizard_payload_site_negatives_brands_auto_groups_transaction(
    client_a: AsyncClient, account_a, db_session
):
    today = today_for("IN")
    payload = {
        "ads_account_id": account_a.id,
        "name": "Full Wizard Campaign",
        "targeting_type": "AUTO",
        "site": "AMAZON_BUSINESS",
        "start_date": today.isoformat(),
        "daily_budget": 500.0,
        "product_ids": [1],
        "ad_groups": [{
            "name": "Auto AG",
            "default_bid": 10.0,
            "keywords": [],
            "targets": [
                {"kind": "AUTO_GROUP", "value": "CLOSE_MATCH", "bid": 12.0},
                {"kind": "AUTO_GROUP", "value": "LOOSE_MATCH", "bid": 8.0}
            ]
        }],
        "negative_keywords": [{"keyword_text": "free items", "match_type": "NEGATIVE_EXACT"}],
        "negative_asins": ["B012345678"],
        "negative_brands": ["BrandX", "BrandY"]
    }

    resp = await client_a.post("/campaigns", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "Full Wizard Campaign"
    assert data["status_label"] == "ENABLED"

    c = (await db_session.execute(select(Campaign).where(Campaign.id == data["id"]))).scalars().first()
    assert c.site == "AMAZON_BUSINESS"
    assert c.targeting_type == "AUTO"

    targets = (await db_session.execute(select(Target))).scalars().all()
    target_kinds = [t.kind for t in targets]
    assert "AUTO_GROUP" in target_kinds
    assert "NEGATIVE_ASIN" in target_kinds
    assert "NEGATIVE_BRAND" in target_kinds

    brands = [t.value for t in targets if t.kind == "NEGATIVE_BRAND"]
    assert "BrandX" in brands and "BrandY" in brands

@pytest.mark.asyncio
async def test_foreign_ids_404_for_new_routes(
    client_a: AsyncClient, client_b: AsyncClient, account_a, account_b, db_session
):
    today = today_for("IN")
    c = Campaign(ads_account_id=account_a.id, name="User A Camp", start_date=today, daily_budget=100.0)
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)

    ag = AdGroup(campaign_id=c.id, name="AG A", default_bid=10.0)
    db_session.add(ag)
    await db_session.commit()
    await db_session.refresh(ag)

    kw = Keyword(ad_group_id=ag.id, keyword_text="test kw", match_type="BROAD", bid=5.0)
    db_session.add(kw)
    await db_session.commit()
    await db_session.refresh(kw)

    # User B cannot access keywords/ag of User A
    resp = await client_b.patch(f"/keywords/{kw.id}", json={"bid": 6.0})
    assert resp.status_code == 404

    resp = await client_b.patch(f"/ad-groups/{ag.id}", json={"name": "Hacked"})
    assert resp.status_code == 404

    resp = await client_b.get(f"/campaigns/{c.id}/negative-keywords")
    assert resp.status_code == 404

    resp = await client_b.post(f"/campaigns/{c.id}/negative-keywords", json={"keyword_text": "bad", "match_type": "NEGATIVE_EXACT"})
    assert resp.status_code == 404

@pytest.mark.asyncio
async def test_bid_suggestions_deterministic(client_a: AsyncClient, account_a):
    payload = {
        "ads_account_id": account_a.id,
        "keywords": [
            {"keyword_text": "running shoes", "match_type": "BROAD"},
            {"keyword_text": "yoga mat", "match_type": "EXACT"}
        ]
    }
    resp1 = await client_a.post("/campaigns/bid-suggestions", json=payload)
    assert resp1.status_code == 200
    data1 = resp1.json()

    resp2 = await client_a.post("/campaigns/bid-suggestions", json=payload)
    assert resp2.status_code == 200
    data2 = resp2.json()

    # Exact deterministic match
    assert data1 == data2
    for item in data1["items"]:
        assert item["low"] <= item["median"] <= item["high"]
        assert item["low"] >= 2.0 # IN min_bid

@pytest.mark.asyncio
async def test_forecast_limits_and_null_ranges(client_a: AsyncClient, account_a):
    # Missing required inputs -> null ranges
    incomplete_payload = {
        "ads_account_id": account_a.id,
        "product_ids": [],
        "daily_budget": None
    }
    resp = await client_a.post("/campaigns/forecast", json=incomplete_payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["clicks"] is None
    assert data["spend"] is None

    # Complete payload -> forecast respects daily_budget * 7
    daily_budget = 100.0
    complete_payload = {
        "ads_account_id": account_a.id,
        "product_ids": [1],
        "daily_budget": daily_budget,
        "default_bid": 5.0,
        "targeting_type": "AUTO",
        "target_count": 4
    }
    resp = await client_a.post("/campaigns/forecast", json=complete_payload)
    assert resp.status_code == 200
    f_data = resp.json()
    assert f_data["spend"]["high"] <= daily_budget * 7
    assert f_data["spend"]["low"] <= f_data["spend"]["high"]
    assert f_data["clicks"]["low"] <= f_data["clicks"]["high"]

@pytest.mark.asyncio
async def test_negative_suggestions_excludes_sales(client_a: AsyncClient, account_a, db_session):
    today = today_for("IN")
    c = Campaign(ads_account_id=account_a.id, name="SearchTerm Camp", start_date=today)
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)

    ag = AdGroup(campaign_id=c.id, name="AG", default_bid=10.0)
    db_session.add(ag)
    await db_session.commit()
    await db_session.refresh(ag)

    kw = Keyword(ad_group_id=ag.id, keyword_text="kw", match_type="BROAD")
    db_session.add(kw)
    await db_session.commit()
    await db_session.refresh(kw)

    # st1: 15 clicks, 0 sales -> wasteful
    st1 = SearchTerm(
        campaign_id=c.id, ad_group_id=ag.id, keyword_id=kw.id,
        search_term="wasteful term", clicks=15, impressions=100, spend=75.0, sales=0.0,
        date=today - timedelta(days=5)
    )
    # st2: 20 clicks, 500 sales -> profitable (should be excluded)
    st2 = SearchTerm(
        campaign_id=c.id, ad_group_id=ag.id, keyword_id=kw.id,
        search_term="profitable term", clicks=20, impressions=200, spend=100.0, sales=500.0,
        date=today - timedelta(days=5)
    )
    db_session.add_all([st1, st2])
    await db_session.commit()

    resp = await client_a.get(f"/campaigns/negative-suggestions?ads_account_id={account_a.id}")
    assert resp.status_code == 200
    suggestions = resp.json()
    term_texts = [s["keyword_text"] for s in suggestions]
    assert "wasteful term" in term_texts
    assert "profitable term" not in term_texts

@pytest.mark.asyncio
async def test_products_search_pagination_sort_q(client_a: AsyncClient, account_a, db_session):
    # Add products with known SKU, title, price
    p1 = Product(ads_account_id=account_a.id, asin="B0ALPHA001", sku="SKU-AAA", title="Wireless Earbuds Alpha", price=50.0, rating_avg=4.5, review_count=100)
    p2 = Product(ads_account_id=account_a.id, asin="B0BETA0002", sku="SKU-BBB", title="Bluetooth Speaker Beta", price=150.0, rating_avg=4.0, review_count=20)
    p3 = Product(ads_account_id=account_a.id, asin="B0GAMMA003", sku="SKU-CCC", title="Noise Cancelling Headphones", price=250.0, rating_avg=4.8, review_count=500)
    db_session.add_all([p1, p2, p3])
    await db_session.commit()

    # Search by SKU
    resp = await client_a.get(f"/products/search?ads_account_id={account_a.id}&q=sku-aaa")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["sku"] == "SKU-AAA"

    # Search by ASIN
    resp = await client_a.get(f"/products/search?ads_account_id={account_a.id}&q=b0beta0002")
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1

    # Sort price_desc
    resp = await client_a.get(f"/products/search?ads_account_id={account_a.id}&sort=price_desc")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items[0]["price"] >= items[1]["price"]

@pytest.mark.asyncio
async def test_drafts_crud_isolation(client_a: AsyncClient, client_b: AsyncClient, account_a, account_b):
    # User A creates a draft
    draft_payload = {
        "ads_account_id": account_a.id,
        "name": "Draft 1",
        "step": 2,
        "payload": {"details": {"name": "Draft 1"}}
    }
    create_resp = await client_a.post("/campaign-drafts", json=draft_payload)
    assert create_resp.status_code == 201
    draft_id = create_resp.json()["id"]

    # User A can list it
    list_a = await client_a.get(f"/campaign-drafts?ads_account_id={account_a.id}")
    assert list_a.status_code == 200
    assert len(list_a.json()) == 1

    # User B cannot see it
    list_b = await client_b.get(f"/campaign-drafts?ads_account_id={account_b.id}")
    assert list_b.status_code == 200
    assert len(list_b.json()) == 0

    # User B cannot update or delete User A's draft
    resp = await client_b.put(f"/campaign-drafts/{draft_id}", json={"name": "Hacked Draft"})
    assert resp.status_code == 404

    resp = await client_b.delete(f"/campaign-drafts/{draft_id}")
    assert resp.status_code == 404

    # User A updates draft
    update_resp = await client_a.put(f"/campaign-drafts/{draft_id}", json={"name": "Draft 1 Renamed", "step": 3})
    assert update_resp.status_code == 200
    assert update_resp.json()["name"] == "Draft 1 Renamed"
    assert update_resp.json()["step"] == 3

    # Oversized payload rejected
    large_payload = {
        "ads_account_id": account_a.id,
        "name": "Big Draft",
        "step": 1,
        "payload": {"huge": "x" * (205 * 1024)}
    }
    resp = await client_a.post("/campaign-drafts", json=large_payload)
    assert resp.status_code == 413

@pytest.mark.asyncio
async def test_products_lookup(client_a: AsyncClient, account_a, db_session):
    p = Product(ads_account_id=account_a.id, asin="B0LOOKUP01", sku="SKU-LOOKUP", title="Lookup Prod", price=30.0)
    db_session.add(p)
    await db_session.commit()

    lookup_payload = {
        "ads_account_id": account_a.id,
        "asins": ["b0lookup01", "SKU-LOOKUP", "NONEXISTENT1", "NONEXISTENT2"]
    }
    resp = await client_a.post("/products/lookup", json=lookup_payload)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["found"]) >= 1
    assert "NONEXISTENT1" in data["not_found"]
    assert "NONEXISTENT2" in data["not_found"]

@pytest.mark.asyncio
async def test_keyword_and_negative_write_endpoints_and_conflicts(
    client_a: AsyncClient, account_a, db_session
):
    today = today_for("IN")
    c = Campaign(ads_account_id=account_a.id, name="KW Test Camp", start_date=today)
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)

    ag = AdGroup(campaign_id=c.id, name="Test AG", default_bid=10.0)
    db_session.add(ag)
    await db_session.commit()
    await db_session.refresh(ag)

    # 1. POST keyword
    resp = await client_a.post("/keywords", json={"ad_group_id": ag.id, "keyword_text": "shoes", "match_type": "EXACT", "bid": 15.0})
    assert resp.status_code == 200
    kw_id = resp.json()["id"]

    # Duplicate rejected
    resp = await client_a.post("/keywords", json={"ad_group_id": ag.id, "keyword_text": "shoes", "match_type": "EXACT"})
    assert resp.status_code == 409

    # 2. Exact negative keyword conflicting with enabled positive EXACT keyword rejected with 422
    resp = await client_a.post(f"/campaigns/{c.id}/negative-keywords", json={"keyword_text": "shoes", "match_type": "NEGATIVE_EXACT"})
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["field"] == "keyword_text"

    # Non-conflicting negative keyword accepted
    resp = await client_a.post(f"/campaigns/{c.id}/negative-keywords", json={"keyword_text": "cheap sandals", "match_type": "NEGATIVE_PHRASE"})
    assert resp.status_code == 200
    nk_id = resp.json()["id"]

    # Duplicate negative keyword rejected with 409
    resp = await client_a.post(f"/campaigns/{c.id}/negative-keywords", json={"keyword_text": "cheap sandals", "match_type": "NEGATIVE_PHRASE"})
    assert resp.status_code == 409

    # DELETE negative keyword works
    del_resp = await client_a.delete(f"/campaigns/{c.id}/negative-keywords/{nk_id}")
    assert del_resp.status_code == 200

@pytest.mark.asyncio
async def test_health_pre_pagination_filtering_and_totals(client_a: AsyncClient, account_a, db_session):
    today = today_for("IN")

    # Campaign 1: Learning (brand new, 2 days old)
    c1 = Campaign(ads_account_id=account_a.id, name="Learning Camp", targeting_type="AUTO", state="ENABLED", start_date=today - timedelta(days=2))
    # Campaign 2: Budget Leak (old, high spend, 0 sales)
    c2 = Campaign(ads_account_id=account_a.id, name="Leak Camp", targeting_type="AUTO", state="ENABLED", start_date=today - timedelta(days=20))
    # Campaign 3: Scale (old, acos <= 30%, 5 orders)
    c3 = Campaign(ads_account_id=account_a.id, name="Scale Camp", targeting_type="AUTO", state="ENABLED", start_date=today - timedelta(days=20))

    db_session.add_all([c1, c2, c3])
    await db_session.commit()

    m1 = MetricDaily(entity_type="campaign", entity_id=c1.id, date=today - timedelta(days=1), impressions=500, clicks=5, spend=10.0, sales=0.0, orders=0)
    m2 = MetricDaily(entity_type="campaign", entity_id=c2.id, date=today - timedelta(days=1), impressions=2000, clicks=50, spend=200.0, sales=0.0, orders=0)
    m3 = MetricDaily(entity_type="campaign", entity_id=c3.id, date=today - timedelta(days=1), impressions=3000, clicks=60, spend=100.0, sales=600.0, orders=5)
    db_session.add_all([m1, m2, m3])
    await db_session.commit()

    # Query with health=BUDGET_LEAK and page_size=1
    resp = await client_a.get(f"/campaigns/report?ads_account_id={account_a.id}&health=BUDGET_LEAK&page=1&page_size=1")
    assert resp.status_code == 200
    data = resp.json()

    # Pre-pagination filtering: total_count must be exactly 1, not 3!
    assert data["total_count"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["health"] == "BUDGET_LEAK"
    # Totals must be computed over the filtered set (spend 200)
    assert data["totals"]["spend"] == 200.0
    assert data["totals"]["sales"] == 0.0

@pytest.mark.asyncio
async def test_summary_compare_aligned_series(client_a: AsyncClient, account_a):
    resp = await client_a.get(f"/campaigns/summary?ads_account_id={account_a.id}&granularity=day&compare=true")
    assert resp.status_code == 200
    data = resp.json()
    assert "series" in data
    assert "previous_series" in data
    assert len(data["series"]) == len(data["previous_series"])

@pytest.mark.asyncio
async def test_health_rules_validation_and_first_match(client_a: AsyncClient, account_a):
    # Invalid metric in health rules -> 422 with field mentioning the metric
    bad_payload = {
        "acos_low": 20.0,
        "acos_high": 90.0,
        "default_target_acos": 30.0,
        "health_rules": [
            {
                "key": "CUSTOM_RULE",
                "name": "Custom",
                "color": "red",
                "enabled": True,
                "match": "ALL",
                "conditions": [{"metric": "invalid_metric_xyz", "op": ">", "value": 10}]
            }
        ]
    }
    resp = await client_a.put("/campaigns/settings/campaigns", json=bad_payload)
    assert resp.status_code == 422
    err_fields = [e.get("field", "") for e in resp.json()["detail"]]
    assert any("metric" in f for f in err_fields)

    # Valid health rules update
    valid_payload = {
        "acos_low": 15.0,
        "acos_high": 85.0,
        "default_target_acos": 25.0,
        "health_rules": [
            {
                "key": "SUPER_HERO",
                "name": "Super Hero",
                "color": "green",
                "enabled": True,
                "match": "ALL",
                "conditions": [{"metric": "orders", "op": ">=", "value": 1}]
            }
        ]
    }
    resp = await client_a.put("/campaigns/settings/campaigns", json=valid_payload)
    assert resp.status_code == 200
    assert resp.json()["health_rules"][0]["key"] == "SUPER_HERO"
