import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.saved_filter import SavedFilter


@pytest.mark.asyncio
async def test_saved_filters_crud_and_isolation(client_a: AsyncClient, client_b: AsyncClient, db_session: AsyncSession):
    # 1. User A lists saved filters (initially empty)
    resp = await client_a.get("/campaigns/saved-filters")
    assert resp.status_code == 200
    assert resp.json() == []

    # 2. User A creates a saved filter
    payload = {
        "name": "High Spend Scaling",
        "filters": {"status": ["ENABLED"], "min_spend": 500, "min_roas": 3.0}
    }
    resp = await client_a.post("/campaigns/saved-filters", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "High Spend Scaling"
    assert data["filters"]["min_spend"] == 500
    filter_id = data["id"]

    # 3. User B cannot see User A's saved filter (isolation)
    resp_b = await client_b.get("/campaigns/saved-filters")
    assert resp_b.status_code == 200
    assert resp_b.json() == []

    # 4. User B cannot delete User A's saved filter
    resp_b_del = await client_b.delete(f"/campaigns/saved-filters/{filter_id}")
    assert resp_b_del.status_code == 404

    # 5. User A deletes their saved filter
    resp_a_del = await client_a.delete(f"/campaigns/saved-filters/{filter_id}")
    assert resp_a_del.status_code == 204

    # 6. Verify deleted
    resp_check = await client_a.get("/campaigns/saved-filters")
    assert resp_check.status_code == 200
    assert resp_check.json() == []
