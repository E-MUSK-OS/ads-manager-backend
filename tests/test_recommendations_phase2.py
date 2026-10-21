import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.campaign import Campaign
from app.models.recommendation_rule import RecommendationRule, RecommendationRuleCampaign
from app.models.user import User


@pytest.mark.asyncio
async def test_meta_endpoint(client_a: AsyncClient, client_b: AsyncClient, account_a, account_b):
    # Meta for India (IN)
    resp_in = await client_a.get(f"/recommendations/meta?ads_account_id={account_a.id}")
    assert resp_in.status_code == 200, resp_in.text
    data_in = resp_in.json()
    assert data_in["currency"]["symbol"] == "₹"
    assert data_in["currency"]["code"] == "INR"
    assert data_in["affected_parameters"]["PLACEMENT"]["available"] is True

    t_in = next(t for t in data_in["templates"] if t["key"] == "negate_wasted_terms")
    spend_cond_in = next(c for c in t_in["conditions"] if c["metric"] == "spend")
    assert spend_cond_in["value"] == 20

    # Meta for US
    resp_us = await client_b.get(f"/recommendations/meta?ads_account_id={account_b.id}")
    assert resp_us.status_code == 200, resp_us.text
    data_us = resp_us.json()
    assert data_us["currency"]["symbol"] == "$"
    assert data_us["currency"]["code"] == "USD"

    t_us = next(t for t in data_us["templates"] if t["key"] == "negate_wasted_terms")
    spend_cond_us = next(c for c in t_us["conditions"] if c["metric"] == "spend")
    assert spend_cond_us["value"] == 10

    # Foreign account access returns 404
    resp_foreign = await client_a.get(f"/recommendations/meta?ads_account_id={account_b.id}")
    assert resp_foreign.status_code == 404


@pytest.mark.asyncio
async def test_validator_errors_carry_correct_fields(client_a: AsyncClient, account_a, db_session: AsyncSession):
    base_payload = {
        "ads_account_id": account_a.id,
        "name": "Test Rule Validation",
        "affected_parameter": "TARGETING",
        "date_filter": "LAST_30_DAYS",
        "min_clicks": 0,
        "conditions": [{"metric": "clicks", "op": "GTE", "value": 10.0}],
        "actions": [{"type": "INCREASE_BID", "params": {"pct": 20}}],
        "scope_mode": "ALL",
        "campaign_ids": [],
    }

    # 1. Unknown metric
    p = {**base_payload, "conditions": [{"metric": "invalid_metric", "op": "GTE", "value": 10.0}]}
    r = await client_a.post("/recommendations/rules", json=p)
    assert r.status_code == 422
    assert any(e["field"] == "conditions.0.metric" for e in r.json()["detail"])

    # 2. Unknown operator
    p = {**base_payload, "conditions": [{"metric": "clicks", "op": "INVALID_OP", "value": 10.0}]}
    r = await client_a.post("/recommendations/rules", json=p)
    assert r.status_code == 422
    assert any(e["field"] == "conditions.0.op" for e in r.json()["detail"])

    # 3. Negative value
    p = {**base_payload, "conditions": [{"metric": "clicks", "op": "GTE", "value": -5.0}]}
    r = await client_a.post("/recommendations/rules", json=p)
    assert r.status_code == 422
    assert any(e["field"] == "conditions.0.value" for e in r.json()["detail"])

    # 4. Percent value > 1000
    p = {**base_payload, "conditions": [{"metric": "acos", "op": "GTE", "value": 1500.0}]}
    r = await client_a.post("/recommendations/rules", json=p)
    assert r.status_code == 422
    assert any(e["field"] == "conditions.0.value" for e in r.json()["detail"])

    # 5. Duplicate condition metric+op pair
    p = {
        **base_payload,
        "conditions": [
            {"metric": "spend", "op": "GTE", "value": 20.0},
            {"metric": "spend", "op": "GTE", "value": 50.0},
        ],
    }
    r = await client_a.post("/recommendations/rules", json=p)
    assert r.status_code == 422
    assert any(e["field"] == "conditions.1" for e in r.json()["detail"])

    # 6. Action not allowed for affected parameter
    p = {**base_payload, "affected_parameter": "CAMPAIGN", "actions": [{"type": "PAUSE_TARGET", "params": {}}]}
    r = await client_a.post("/recommendations/rules", json=p)
    assert r.status_code == 422
    assert any(e["field"] == "actions.0.type" for e in r.json()["detail"])

    # 7. Action param out of range (pct > 90 for DECREASE_BID)
    p = {**base_payload, "actions": [{"type": "DECREASE_BID", "params": {"pct": 95}}]}
    r = await client_a.post("/recommendations/rules", json=p)
    assert r.status_code == 422
    assert any(e["field"] == "actions.0.params.pct" for e in r.json()["detail"])

    # 8. Action bid > max_bid (in IN max_bid is 5000, let's pass 99999)
    p = {
        **base_payload,
        "affected_parameter": "SEARCH_TERM",
        "actions": [{"type": "ADD_AS_TARGETING", "params": {"match_type": "EXACT", "bid": 99999.0}}],
    }
    r = await client_a.post("/recommendations/rules", json=p)
    assert r.status_code == 422
    assert any(e["field"] == "actions.0.params.bid" for e in r.json()["detail"])

    # 9. Conflicting actions (INCREASE_BID and DECREASE_BID)
    p = {
        **base_payload,
        "actions": [
            {"type": "INCREASE_BID", "params": {"pct": 10}},
            {"type": "DECREASE_BID", "params": {"pct": 10}},
        ],
    }
    r = await client_a.post("/recommendations/rules", json=p)
    assert r.status_code == 422
    assert any(e["field"] == "actions" for e in r.json()["detail"])

    # 10. Duplicate action type
    p = {
        **base_payload,
        "actions": [
            {"type": "PAUSE_TARGET", "params": {}},
            {"type": "PAUSE_TARGET", "params": {}},
        ],
    }
    r = await client_a.post("/recommendations/rules", json=p)
    assert r.status_code == 422
    assert any(e["field"] == "actions.1" for e in r.json()["detail"])

    # 11. PLACEMENT enabled in Phase 5 (accepts rule)
    p = {**base_payload, "name": "Placement Test Rule P5", "affected_parameter": "PLACEMENT", "actions": [{"type": "INCREASE_PLACEMENT", "params": {"points": 20}}]}
    r = await client_a.post("/recommendations/rules", json=p)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_duplicate_name_case_insensitive_409(client_a: AsyncClient, account_a):
    payload = {
        "ads_account_id": account_a.id,
        "name": "Negate Wasted Terms",
        "affected_parameter": "SEARCH_TERM",
        "date_filter": "LAST_30_DAYS",
        "min_clicks": 0,
        "conditions": [{"metric": "spend", "op": "GTE", "value": 20.0}],
        "actions": [{"type": "ADD_AS_NEGATIVE", "params": {"match_type": "NEGATIVE_EXACT", "level": "AD_GROUP"}}],
        "scope_mode": "ALL",
        "campaign_ids": [],
    }

    r1 = await client_a.post("/recommendations/rules", json=payload)
    assert r1.status_code == 200, r1.text

    # Exact duplicate
    r2 = await client_a.post("/recommendations/rules", json=payload)
    assert r2.status_code == 409
    assert "already exists" in r2.json()["detail"]

    # Different case duplicate
    p_diff_case = {**payload, "name": "negate wasted terms"}
    r3 = await client_a.post("/recommendations/rules", json=p_diff_case)
    assert r3.status_code == 409
    assert "already exists" in r3.json()["detail"]


@pytest.mark.asyncio
async def test_campaign_scope_validation(client_a: AsyncClient, account_a, account_b, db_session: AsyncSession):
    # Create campaign in account_a
    camp_a = Campaign(ads_account_id=account_a.id, name="Campaign A", state="ENABLED")
    db_session.add(camp_a)
    # Create campaign in account_b
    camp_b = Campaign(ads_account_id=account_b.id, name="Campaign B", state="ENABLED")
    db_session.add(camp_b)
    await db_session.commit()

    base_payload = {
        "ads_account_id": account_a.id,
        "name": "Scoped Rule",
        "affected_parameter": "CAMPAIGN",
        "date_filter": "LAST_7_DAYS",
        "min_clicks": 0,
        "conditions": [{"metric": "spend", "op": "GTE", "value": 50.0}],
        "actions": [{"type": "DECREASE_BUDGET", "params": {"pct": 20}}],
        "scope_mode": "INCLUDE",
        "campaign_ids": [],
    }

    # Empty campaign_ids for INCLUDE
    r = await client_a.post("/recommendations/rules", json=base_payload)
    assert r.status_code == 422
    assert any(e["field"] == "campaign_ids" for e in r.json()["detail"])

    # Campaign belonging to another account (account_b)
    p_foreign = {**base_payload, "campaign_ids": [camp_b.id]}
    r = await client_a.post("/recommendations/rules", json=p_foreign)
    assert r.status_code == 422
    assert any("does not belong to this account" in e["message"] for e in r.json()["detail"])

    # Valid campaign in account_a
    p_valid = {**base_payload, "campaign_ids": [camp_a.id]}
    r = await client_a.post("/recommendations/rules", json=p_valid)
    assert r.status_code == 200, r.text
    rule_id = r.json()["rule"]["id"]
    assert r.json()["rule"]["scope_label"] == "Include 1"
    assert r.json()["rule"]["campaign_ids"] == [camp_a.id]

    # Deleting campaign removes recommendation_rule_campaigns row
    await db_session.delete(camp_a)
    await db_session.commit()

    r_rule = await client_a.get(f"/recommendations/rules/{rule_id}")
    assert r_rule.status_code == 200
    assert r_rule.json()["campaign_ids"] == []


@pytest.mark.asyncio
async def test_foreign_account_and_rule_404s(client_a: AsyncClient, client_b: AsyncClient, account_a, account_b):
    # Create rule in account_b using client_b
    payload_b = {
        "ads_account_id": account_b.id,
        "name": "Account B Rule",
        "affected_parameter": "SEARCH_TERM",
        "date_filter": "LAST_30_DAYS",
        "min_clicks": 0,
        "conditions": [{"metric": "orders", "op": "LTE", "value": 0.0}],
        "actions": [{"type": "ADD_AS_NEGATIVE", "params": {"match_type": "NEGATIVE_EXACT", "level": "AD_GROUP"}}],
        "scope_mode": "ALL",
        "campaign_ids": [],
    }
    res_b = await client_b.post("/recommendations/rules", json=payload_b)
    assert res_b.status_code == 200, res_b.text
    rule_b_id = res_b.json()["rule"]["id"]

    # client_a accessing account_b's rule or account returns 404
    assert (await client_a.get(f"/recommendations/rules?ads_account_id={account_b.id}")).status_code == 404
    assert (await client_a.post(f"/recommendations/rules?ads_account_id={account_b.id}", json=payload_b)).status_code == 404
    assert (await client_a.get(f"/recommendations/rules/{rule_b_id}")).status_code == 404
    assert (await client_a.put(f"/recommendations/rules/{rule_b_id}", json=payload_b)).status_code == 404
    assert (await client_a.delete(f"/recommendations/rules/{rule_b_id}")).status_code == 404
    assert (await client_a.patch(f"/recommendations/rules/{rule_b_id}/active", json={"is_active": False})).status_code == 404
    assert (await client_a.put(f"/recommendations/rules/{rule_b_id}/scope", json={"scope_mode": "ALL", "campaign_ids": []})).status_code == 404
    assert (await client_a.post(f"/recommendations/rules/{rule_b_id}/duplicate")).status_code == 404


@pytest.mark.asyncio
async def test_duplicate_bulk_export_import_roundtrip(client_a: AsyncClient, client_b: AsyncClient, account_a, account_b):
    # Create a rule in account_a
    payload = {
        "ads_account_id": account_a.id,
        "name": "Harvest Convert",
        "affected_parameter": "SEARCH_TERM",
        "date_filter": "LAST_30_DAYS",
        "min_clicks": 0,
        "conditions": [{"metric": "orders", "op": "GTE", "value": 2.0}],
        "actions": [{"type": "ADD_AS_TARGETING", "params": {"match_type": "EXACT"}}],
        "scope_mode": "ALL",
        "campaign_ids": [],
    }
    r = await client_a.post("/recommendations/rules", json=payload)
    assert r.status_code == 200
    rule_id = r.json()["rule"]["id"]

    # 1. Duplicate
    dup_resp = await client_a.post(f"/recommendations/rules/{rule_id}/duplicate")
    assert dup_resp.status_code == 200
    dup_data = dup_resp.json()
    assert dup_data["name"] == "Harvest Convert (copy)"
    assert dup_data["is_active"] is False
    dup_id = dup_data["id"]

    # 2. Bulk Action (mixed IDs, including nonexistent and foreign)
    bulk_resp = await client_a.post(
        "/recommendations/rules/bulk",
        json={"ids": [rule_id, dup_id, 99999], "action": "DISABLE"},
    )
    assert bulk_resp.status_code == 200
    results = {item["id"]: item for item in bulk_resp.json()["results"]}
    assert results[rule_id]["success"] is True
    assert results[dup_id]["success"] is True
    assert results[99999]["success"] is False

    # Check rule_id is now disabled
    rule_check = await client_a.get(f"/recommendations/rules/{rule_id}")
    assert rule_check.json()["is_active"] is False

    # 3. Export
    exp_resp = await client_a.post("/recommendations/rules/export", json={"ids": [rule_id]})
    assert exp_resp.status_code == 200
    exp_data = exp_resp.json()
    assert exp_data["version"] == 1
    assert len(exp_data["rules"]) == 1
    assert exp_data["rules"][0]["name"] == "Harvest Convert"
    assert "id" not in exp_data["rules"][0]
    assert "campaign_ids" not in exp_data["rules"][0]

    # 4. Import back to account_a (Name clash -> skipped)
    imp_resp = await client_a.post(
        "/recommendations/rules/import",
        json={"ads_account_id": account_a.id, "rules": exp_data["rules"]},
    )
    assert imp_resp.status_code == 200
    imp_results = imp_resp.json()["results"]
    assert len(imp_results) == 1
    assert imp_results[0]["status"] == "skipped"
    assert "already exists" in imp_results[0]["reason"]

    # 5. Import to account_b (Should succeed as created, is_active=false, scope=ALL)
    imp_resp_b = await client_b.post(
        "/recommendations/rules/import",
        json={"ads_account_id": account_b.id, "rules": exp_data["rules"]},
    )
    assert imp_resp_b.status_code == 200
    imp_results_b = imp_resp_b.json()["results"]
    assert len(imp_results_b) == 1
    assert imp_results_b[0]["status"] == "created"
    new_b_rule_id = imp_results_b[0]["rule_id"]

    new_rule_b = await client_b.get(f"/recommendations/rules/{new_b_rule_id}")
    assert new_rule_b.status_code == 200
    assert new_rule_b.json()["is_active"] is False
    assert new_rule_b.json()["scope_mode"] == "ALL"


@pytest.mark.asyncio
async def test_preview_wired(client_a: AsyncClient, account_a):
    payload = {
        "ads_account_id": account_a.id,
        "rule": {
            "name": "Draft Rule",
            "affected_parameter": "CAMPAIGN",
            "date_filter": "LAST_7_DAYS",
            "min_clicks": 0,
            "conditions": [{"metric": "spend", "op": "GTE", "value": 10.0}],
            "actions": [{"type": "INCREASE_BUDGET", "params": {"pct": 10}}],
            "scope_mode": "ALL",
            "campaign_ids": [],
        },
    }
    r = await client_a.post("/recommendations/rules/preview", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert "match_count" in data
    assert "evaluated_entities" in data
    assert "sample" in data
