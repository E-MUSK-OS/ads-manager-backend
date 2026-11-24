import pytest
from decimal import Decimal
from datetime import date, timedelta
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.recommendations.engine import pct_change, RuleBasedEngine, Candidate
from app.recommendations.registry import (
    get_visible_affected_parameters,
    get_visible_actions,
    get_visible_templates,
    TEMPLATES,
)
from app.models.ads_account import AdsAccount
from app.models.campaign import Campaign
from app.models.ad_group import AdGroup
from app.models.keyword import Keyword
from app.models.metric_daily import MetricDaily
from app.models.recommendation_rule import RecommendationRule


def test_r1_3_decimal_pct_change_exact_values():
    """Verify R1.3 exact test cases for pct_change using Decimal and ROUND_HALF_UP."""
    # 5.04 -10% -> 4.54
    assert pct_change(5.04, 10, False) == Decimal("4.54")
    # 2.20 -10% -> 1.98
    assert pct_change(2.20, 10, False) == Decimal("1.98")
    # 2.77 -10% -> 2.49
    assert pct_change(2.77, 10, False) == Decimal("2.49")
    # 3.05 -10% -> 2.75
    assert pct_change(3.05, 10, False) == Decimal("2.75")
    # 3.35 -3% -> 3.25
    assert pct_change(3.35, 3, False) == Decimal("3.25")
    # Also verify positive increase: 5.04 +10% -> 5.54 (5.04 * 1.10 = 5.544 -> 5.54)
    assert pct_change(5.04, 10, True) == Decimal("5.54")


def test_r1_2_clamp_at_min_bid_skipped():
    """Verify that when decrease bid clamps to min_bid, candidate action is skipped (no-op)."""
    engine = RuleBasedEngine()
    m_rules = {"min_bid": 1.0, "max_bid": 5000.0}

    # Bid at 1.00 decreased by 10% clamps at 1.00 -> from == to -> should return empty list
    current_info = {"bid": 1.00, "state": "ENABLED"}
    actions = [{"type": "DECREASE_BID", "params": {"pct": 10}}]
    suggested = engine._build_suggested_actions(actions, "TARGETING", current_info, m_rules)
    assert suggested == []


def test_r1_2_bid_unknown_skipped():
    """Verify that when target has no bid and ad group has no default bid, candidate is skipped."""
    engine = RuleBasedEngine()
    m_rules = {"min_bid": 1.0, "max_bid": 5000.0}

    current_info = {"bid": None, "state": "ENABLED"}
    actions = [{"type": "DECREASE_BID", "params": {"pct": 10}}]
    suggested = engine._build_suggested_actions(actions, "TARGETING", current_info, m_rules)
    assert suggested == []


def test_r1_2_budget_clamp_at_min_budget_skipped():
    """Verify that when decrease budget clamps to min_budget, candidate action is skipped."""
    engine = RuleBasedEngine()
    m_rules = {"min_budget": 50.0, "max_daily_budget": 20000000.0}

    current_info = {"daily_budget": 50.00, "state": "ENABLED"}
    actions = [{"type": "DECREASE_BUDGET", "params": {"pct": 10}}]
    suggested = engine._build_suggested_actions(actions, "CAMPAIGN", current_info, m_rules)
    assert suggested == []


def test_r1_4_placement_hidden_by_default():
    """Verify PLACEMENT is hidden in visible parameters, actions, and templates when off."""
    params = get_visible_affected_parameters()
    assert "PLACEMENT" not in params

    actions = get_visible_actions()
    assert "PLACEMENT" not in actions
    # ADD_AS_TARGETING hidden per Q1
    assert "ADD_AS_TARGETING" not in actions.get("SEARCH_TERM", {})

    templates = get_visible_templates()
    assert not any(t["affected_parameter"] == "PLACEMENT" for t in templates)


def test_r1_5_templates_have_empty_thresholds():
    """Verify gallery templates have empty thresholds (None) for user to enter."""
    for t in TEMPLATES:
        for cond in t["conditions"]:
            # Bleeding targets or negate terms templates have None threshold values
            if t["key"] in ("negate_wasted_terms", "pause_bleeding_targets", "lower_bids_high_acos"):
                assert cond["value"] is None
