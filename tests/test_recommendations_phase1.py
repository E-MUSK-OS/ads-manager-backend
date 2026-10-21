from datetime import date, timedelta
import pytest
from sqlalchemy import select, func, Float
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.marketplace_rules import today_for
from app.models.ads_account import AdsAccount
from app.models.campaign import Campaign
from app.models.keyword import Keyword
from app.models.search_term import SearchTerm
from app.models.metric_daily import MetricDaily
from app.models.user import User
from app.providers.ads.mock_provider import MockAdsProvider
from app.recommendations.registry import (
    DATE_FILTERS,
    METRICS,
    OPERATORS,
    AFFECTED_PARAMETERS,
    ACTIONS,
    CONFLICTS,
    TEMPLATES,
    LIMITS,
    resolve_value,
    resolve_template,
)
from app.recommendations.dates import resolve_date_filter
from app.services.metrics_service import derive


def test_registry_sanity():
    """Verify registry consistency: every action's params have a type, templates are valid, limits are defined."""
    # Check limits
    assert LIMITS["max_conditions"] == 6
    assert LIMITS["max_actions"] == 4
    assert LIMITS["max_rules_per_account"] == 50
    assert LIMITS["name_max"] == 60

    # Check metrics and operators
    for m_key, (m_label, m_unit) in METRICS.items():
        assert isinstance(m_label, str)
        assert m_unit in ("count", "money", "percent", "ratio")

    for op_key, (op_label, op_sym) in OPERATORS.items():
        assert isinstance(op_label, str)
        assert isinstance(op_sym, str)

    # Check actions per affected parameter
    allowed_param_types = {"enum", "money", "percent", "int"}
    all_action_types = set()

    for param_key, param_label in AFFECTED_PARAMETERS.items():
        assert param_key in ACTIONS, f"Missing actions for parameter: {param_key}"
        param_actions = ACTIONS[param_key]
        for act_key, act_def in param_actions.items():
            all_action_types.add(act_key)
            assert "label" in act_def
            assert "params" in act_def
            for p_name, p_schema in act_def["params"].items():
                assert "type" in p_schema, f"Action {act_key} param {p_name} has no type"
                assert p_schema["type"] in allowed_param_types
                assert "label" in p_schema
                if p_schema["type"] == "enum":
                    assert "options" in p_schema and len(p_schema["options"]) > 0
                    assert "default" in p_schema and p_schema["default"] in p_schema["options"]
                elif p_schema["type"] in ("percent", "int"):
                    assert "min" in p_schema and "max" in p_schema
                    assert p_schema["min"] <= p_schema["max"]

    # Check conflicts refer to real action types
    for conflict_pair in CONFLICTS:
        assert len(conflict_pair) == 2
        for act in conflict_pair:
            assert act in all_action_types, f"Conflict references unknown action: {act}"

    # Check templates
    for t in TEMPLATES:
        assert "key" in t and "name" in t and "affected_parameter" in t
        assert t["affected_parameter"] in AFFECTED_PARAMETERS
        assert t["date_filter"] in DATE_FILTERS
        assert len(t["conditions"]) >= 1
        assert len(t["actions"]) >= 1
        for cond in t["conditions"]:
            assert cond["metric"] in METRICS
            assert cond["op"] in OPERATORS
        for act in t["actions"]:
            assert act["type"] in ACTIONS[t["affected_parameter"]]


def test_resolve_value():
    """Verify resolve_value for marketplace-specific dictionaries and primitives."""
    val_dict = {"default": 10, "IN": 20}
    assert resolve_value(val_dict, "IN") == 20
    assert resolve_value(val_dict, "US") == 10
    assert resolve_value(val_dict, "UK") == 10

    # Primitives
    assert resolve_value(50, "IN") == 50
    assert resolve_value("test", "US") == "test"
    assert resolve_value(0.0, "IN") == 0.0

    # Template resolution
    t = TEMPLATES[0]
    resolved_in = resolve_template(t, "IN")
    spend_cond_in = next(c for c in resolved_in["conditions"] if c["metric"] == "spend")
    assert spend_cond_in["value"] == 20

    resolved_us = resolve_template(t, "US")
    spend_cond_us = next(c for c in resolved_us["conditions"] if c["metric"] == "spend")
    assert spend_cond_us["value"] == 10


def test_date_filters_resolution():
    """Verify resolution of all 10 date filters, including boundaries."""
    # Mid-month, Wednesday
    today = date(2026, 10, 21)

    # 1. TODAY: [t, t]
    assert resolve_date_filter("TODAY", today) == (today, today)

    # 2. YESTERDAY: [t-1, t-1]
    assert resolve_date_filter("YESTERDAY", today) == (date(2026, 10, 20), date(2026, 10, 20))

    # 3. LAST_7_DAYS: [t-6, t]
    assert resolve_date_filter("LAST_7_DAYS", today) == (date(2026, 10, 15), date(2026, 10, 21))

    # 4. THIS_WEEK: Monday..today
    # 2026-10-21 is Wednesday (weekday=2) -> Monday is 2026-10-19
    assert resolve_date_filter("THIS_WEEK", today) == (date(2026, 10, 19), date(2026, 10, 21))

    # 5. LAST_WEEK: previous Monday..Sunday
    assert resolve_date_filter("LAST_WEEK", today) == (date(2026, 10, 12), date(2026, 10, 18))

    # 6. LAST_30_DAYS: [t-29, t]
    assert resolve_date_filter("LAST_30_DAYS", today) == (date(2026, 9, 22), date(2026, 10, 21))

    # 7. THIS_MONTH: 1st of month..today
    assert resolve_date_filter("THIS_MONTH", today) == (date(2026, 10, 1), date(2026, 10, 21))

    # 8. LAST_MONTH: 1st of previous month..last day of previous month
    assert resolve_date_filter("LAST_MONTH", today) == (date(2026, 9, 1), date(2026, 9, 30))

    # 9. YEAR_TO_DATE: Jan 1..today
    assert resolve_date_filter("YEAR_TO_DATE", today) == (date(2026, 1, 1), date(2026, 10, 21))

    # 10. LIFETIME: [2000-01-01, today]
    assert resolve_date_filter("LIFETIME", today) == (date(2000, 1, 1), date(2026, 10, 21))

    # Boundary tests
    # Monday: THIS_WEEK should start and end on Monday
    monday = date(2026, 10, 19)
    assert resolve_date_filter("THIS_WEEK", monday) == (monday, monday)

    # Sunday: THIS_WEEK spans full week
    sunday = date(2026, 10, 25)
    assert resolve_date_filter("THIS_WEEK", sunday) == (date(2026, 10, 19), sunday)

    # January 1: LAST_MONTH must correctly roll back year to December of prior year
    jan1 = date(2026, 1, 1)
    assert resolve_date_filter("THIS_MONTH", jan1) == (jan1, jan1)
    assert resolve_date_filter("LAST_MONTH", jan1) == (date(2025, 12, 1), date(2025, 12, 31))
    assert resolve_date_filter("YEAR_TO_DATE", jan1) == (jan1, jan1)

    # Unknown key raises ValueError
    with pytest.raises(ValueError):
        resolve_date_filter("UNKNOWN_FILTER", today)


@pytest.mark.asyncio
async def test_mock_account_has_search_terms_and_template_candidates(db_session: AsyncSession, user_a: User):
    """
    Verify fresh mock account produces:
    - >= 3 zero-order search terms with spend
    - coherent orders (sales > 0 => orders >= 1; orders == 0 => sales == 0)
    - >= 1 candidate for each of the 6 non-placement templates when evaluated
    """
    account = AdsAccount(user_id=user_a.id, marketplace="IN", is_mock=True)
    db_session.add(account)
    await db_session.commit()

    provider = MockAdsProvider(db_session)
    await provider.generate_for_new_connection(account.id)

    today = today_for(account.marketplace)

    # 1. Check Search Terms
    st_rows = (await db_session.execute(
        select(SearchTerm).join(Campaign).where(Campaign.ads_account_id == account.id)
    )).scalars().all()
    assert len(st_rows) >= 8

    # Coherence check
    for st in st_rows:
        if st.orders == 0:
            assert st.sales == 0.0, f"Search term '{st.search_term}' has 0 orders but sales={st.sales}"
        else:
            assert st.orders >= 1
            assert st.sales > 0.0, f"Search term '{st.search_term}' has orders={st.orders} but 0 sales"

    # Zero-order search terms with spend >= 20
    zero_order_terms = [st for st in st_rows if st.orders == 0 and st.spend >= 20.0]
    assert len(zero_order_terms) >= 3, f"Expected >= 3 zero-order terms with spend >= 20, found {len(zero_order_terms)}"

    # Converting search terms with orders >= 2 and ACOS <= 30%
    converting_terms = [st for st in st_rows if st.orders >= 2 and (st.spend / st.sales * 100) <= 30.0]
    assert len(converting_terms) >= 1, f"Expected >= 1 converting search terms, found {len(converting_terms)}"

    # 2. Check Candidate Evaluation for All 6 Non-Placement Templates

    # Template 1: negate_wasted_terms (SEARCH_TERM, LAST_30_DAYS, spend >= 20, orders <= 0)
    start_30, end_30 = resolve_date_filter("LAST_30_DAYS", today)
    t1_matches = (await db_session.execute(
        select(SearchTerm.search_term, func.sum(SearchTerm.spend), func.sum(SearchTerm.orders))
        .join(Campaign)
        .where(
            Campaign.ads_account_id == account.id,
            SearchTerm.date >= start_30,
            SearchTerm.date <= end_30
        )
        .group_by(SearchTerm.search_term)
        .having(func.sum(SearchTerm.spend) >= 20.0, func.sum(SearchTerm.orders) <= 0)
    )).all()
    assert len(t1_matches) >= 1, "Template negate_wasted_terms has 0 candidates"

    # Template 2: harvest_converting_terms (SEARCH_TERM, LAST_30_DAYS, orders >= 2, acos <= 30)
    t2_matches = (await db_session.execute(
        select(
            SearchTerm.search_term,
            func.sum(SearchTerm.orders).label("orders"),
            (func.sum(SearchTerm.spend) / func.nullif(func.sum(SearchTerm.sales), 0) * 100).label("acos")
        )
        .join(Campaign)
        .where(
            Campaign.ads_account_id == account.id,
            SearchTerm.date >= start_30,
            SearchTerm.date <= end_30
        )
        .group_by(SearchTerm.search_term)
        .having(
            func.sum(SearchTerm.orders) >= 2,
            (func.sum(SearchTerm.spend) / func.nullif(func.sum(SearchTerm.sales), 0) * 100) <= 30.0
        )
    )).all()
    assert len(t2_matches) >= 1, "Template harvest_converting_terms has 0 candidates"

    # Template 3: pause_bleeding_targets (TARGETING, LAST_30_DAYS, clicks >= 20, orders <= 0)
    t3_matches = (await db_session.execute(
        select(Keyword.id, func.sum(MetricDaily.clicks).label("clicks"), func.sum(MetricDaily.orders).label("orders"))
        .join(MetricDaily, MetricDaily.entity_id == Keyword.id)
        .join(Campaign, MetricDaily.entity_type == "keyword")
        .where(
            Campaign.ads_account_id == account.id,
            MetricDaily.entity_type == "keyword",
            MetricDaily.date >= start_30,
            MetricDaily.date <= end_30
        )
        .group_by(Keyword.id)
        .having(func.sum(MetricDaily.clicks) >= 20, func.sum(MetricDaily.orders) <= 0)
    )).all()
    assert len(t3_matches) >= 1, "Template pause_bleeding_targets has 0 candidates"

    # Template 4: lower_bids_high_acos (TARGETING, LAST_7_DAYS, clicks >= 10, acos >= 50, orders >= 1)
    start_7, end_7 = resolve_date_filter("LAST_7_DAYS", today)
    t4_matches = (await db_session.execute(
        select(
            Keyword.id,
            func.sum(MetricDaily.clicks).label("clicks"),
            func.sum(MetricDaily.orders).label("orders"),
            (func.sum(MetricDaily.spend) / func.nullif(func.sum(MetricDaily.sales), 0) * 100).label("acos")
        )
        .join(MetricDaily, MetricDaily.entity_id == Keyword.id)
        .join(Campaign, MetricDaily.entity_type == "keyword")
        .where(
            Campaign.ads_account_id == account.id,
            MetricDaily.entity_type == "keyword",
            MetricDaily.date >= start_7,
            MetricDaily.date <= end_7
        )
        .group_by(Keyword.id)
        .having(
            func.sum(MetricDaily.clicks) >= 10,
            func.sum(MetricDaily.orders) >= 1,
            (func.sum(MetricDaily.spend) / func.nullif(func.sum(MetricDaily.sales), 0) * 100) >= 50.0
        )
    )).all()
    assert len(t4_matches) >= 1, "Template lower_bids_high_acos has 0 candidates"

    # Template 5: raise_bids_efficient (TARGETING, LAST_30_DAYS, clicks >= 10, acos <= 15, orders >= 3)
    t5_matches = (await db_session.execute(
        select(
            Keyword.id,
            func.sum(MetricDaily.clicks).label("clicks"),
            func.sum(MetricDaily.orders).label("orders"),
            (func.sum(MetricDaily.spend) / func.nullif(func.sum(MetricDaily.sales), 0) * 100).label("acos")
        )
        .join(MetricDaily, MetricDaily.entity_id == Keyword.id)
        .join(Campaign, MetricDaily.entity_type == "keyword")
        .where(
            Campaign.ads_account_id == account.id,
            MetricDaily.entity_type == "keyword",
            MetricDaily.date >= start_30,
            MetricDaily.date <= end_30
        )
        .group_by(Keyword.id)
        .having(
            func.sum(MetricDaily.clicks) >= 10,
            func.sum(MetricDaily.orders) >= 3,
            (func.sum(MetricDaily.spend) / func.nullif(func.sum(MetricDaily.sales), 0) * 100) <= 15.0
        )
    )).all()
    assert len(t5_matches) >= 1, "Template raise_bids_efficient has 0 candidates"

    # Template 6: scale_winning_campaigns (CAMPAIGN, LAST_7_DAYS, roas >= 4, orders >= 5)
    t6_matches = (await db_session.execute(
        select(
            Campaign.id,
            func.sum(MetricDaily.orders).label("orders"),
            (func.sum(MetricDaily.sales) / func.nullif(func.sum(MetricDaily.spend), 0)).label("roas")
        )
        .join(MetricDaily, MetricDaily.entity_id == Campaign.id)
        .where(
            Campaign.ads_account_id == account.id,
            MetricDaily.entity_type == "campaign",
            MetricDaily.date >= start_7,
            MetricDaily.date <= end_7
        )
        .group_by(Campaign.id)
        .having(
            func.sum(MetricDaily.orders) >= 5,
            (func.sum(MetricDaily.sales) / func.nullif(func.sum(MetricDaily.spend), 0)) >= 4.0
        )
    )).all()
    assert len(t6_matches) >= 1, "Template scale_winning_campaigns has 0 candidates"

    # Template 7: cut_leaking_campaigns (CAMPAIGN, LAST_7_DAYS, acos >= 80, spend >= 300 (IN))
    t7_matches = (await db_session.execute(
        select(
            Campaign.id,
            func.sum(MetricDaily.spend).label("spend"),
            (func.sum(MetricDaily.spend) / func.nullif(func.sum(MetricDaily.sales), 0) * 100).label("acos")
        )
        .join(MetricDaily, MetricDaily.entity_id == Campaign.id)
        .where(
            Campaign.ads_account_id == account.id,
            MetricDaily.entity_type == "campaign",
            MetricDaily.date >= start_7,
            MetricDaily.date <= end_7
        )
        .group_by(Campaign.id)
        .having(
            func.sum(MetricDaily.spend) >= 300.0,
            (func.sum(MetricDaily.spend) / func.nullif(func.sum(MetricDaily.sales), 0) * 100) >= 80.0
        )
    )).all()
    assert len(t7_matches) >= 1, "Template cut_leaking_campaigns has 0 candidates"
