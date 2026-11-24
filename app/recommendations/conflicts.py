from __future__ import annotations

from typing import Any
from app.recommendations.registry import CONFLICTS

ACTION_FIELD_MAP = {
    "INCREASE_BID": "bid",
    "DECREASE_BID": "bid",
    "PAUSE_TARGET": "state",
    "INCREASE_BUDGET": "daily_budget",
    "DECREASE_BUDGET": "daily_budget",
    "PAUSE_CAMPAIGN": "state",
    "INCREASE_PLACEMENT": "placement_modifier",
    "DECREASE_PLACEMENT": "placement_modifier",
    "ADD_AS_NEGATIVE": "negative_search_term",
    "ADD_AS_TARGETING": "positive_target",
}


def are_conflicting_actions(type_a: str, type_b: str) -> bool:
    if not type_a or not type_b:
        return False
    # Check CONFLICTS pairs (opposing directions or incompatible intents)
    pair = {type_a, type_b}
    for c_set in CONFLICTS:
        if pair == c_set:
            return True
    # Check same field modified twice (e.g. two DECREASE_BID or two INCREASE_BUDGET)
    field_a = ACTION_FIELD_MAP.get(type_a)
    field_b = ACTION_FIELD_MAP.get(type_b)
    if field_a and field_b and field_a == field_b:
        return True
    return False


def are_same_entity(r1: Any, r2: Any) -> bool:
    # Must belong to same account
    acc1 = getattr(r1, "ads_account_id", None)
    acc2 = getattr(r2, "ads_account_id", None)
    if acc1 is not None and acc2 is not None and acc1 != acc2:
        return False

    t1 = getattr(r1, "entity_type", "")
    t2 = getattr(r2, "entity_type", "")
    if t1 != t2:
        return False

    if t1 == "search_term":
        # Search terms are identified by campaign + clean normalized text
        return (
            getattr(r1, "campaign_id", None) == getattr(r2, "campaign_id", None)
            and str(getattr(r1, "entity_label", "")).strip().lower()
            == str(getattr(r2, "entity_label", "")).strip().lower()
        )
    else:
        # Campaign, target, keyword, placement identified by entity_id
        id1 = getattr(r1, "entity_id", None)
        id2 = getattr(r2, "entity_id", None)
        return id1 is not None and id1 == id2


def are_conflicting_recommendations(r1: Any, r2: Any) -> bool:
    id1 = getattr(r1, "id", None)
    id2 = getattr(r2, "id", None)
    if id1 is not None and id2 is not None and id1 == id2:
        return False

    if not are_same_entity(r1, r2):
        return False

    actions1 = getattr(r1, "suggested_actions", []) or []
    actions2 = getattr(r2, "suggested_actions", []) or []

    for a1 in actions1:
        t1 = a1.get("type") if isinstance(a1, dict) else getattr(a1, "type", None)
        for a2 in actions2:
            t2 = a2.get("type") if isinstance(a2, dict) else getattr(a2, "type", None)
            if are_conflicting_actions(t1, t2):
                return True
    return False


def detect_conflicts(recs: list[Any]) -> dict[int, list[int]]:
    """
    Returns map of rec.id -> list of conflicting rec.ids among the provided list.
    """
    conflicts: dict[int, list[int]] = {
        r.id: [] for r in recs if getattr(r, "id", None) is not None
    }
    n = len(recs)
    for i in range(n):
        r1 = recs[i]
        id1 = getattr(r1, "id", None)
        if id1 is None:
            continue
        for j in range(i + 1, n):
            r2 = recs[j]
            id2 = getattr(r2, "id", None)
            if id2 is None:
                continue
            if are_conflicting_recommendations(r1, r2):
                conflicts[id1].append(id2)
                conflicts[id2].append(id1)
    return conflicts


def check_rule_contradictions(conditions: list[dict[str, Any] | Any]) -> list[str]:
    """
    Checks for contradictory conditions within a single rule (Q14).
    E.g. orders >= 5 AND orders <= 0.
    """
    warnings: list[str] = []
    by_metric: dict[str, list[tuple[str, float]]] = {}

    for c in conditions:
        m = c.get("metric") if isinstance(c, dict) else getattr(c, "metric", "")
        op = c.get("op") if isinstance(c, dict) else getattr(c, "op", "")
        val_raw = c.get("value") if isinstance(c, dict) else getattr(c, "value", None)
        if not m or not op or val_raw is None:
            continue
        try:
            val = float(val_raw)
        except (ValueError, TypeError):
            continue
        by_metric.setdefault(m, []).append((str(op).upper(), val))

    for m, cond_list in by_metric.items():
        n = len(cond_list)
        for i in range(n):
            op1, val1 = cond_list[i]
            for j in range(i + 1, n):
                op2, val2 = cond_list[j]
                is_contradiction = False

                # Case 1: GT/GTE vs LT/LTE
                if op1 in ("GT", "GTE") and op2 in ("LT", "LTE"):
                    if op1 == "GT" and op2 in ("LT", "LTE") and val1 >= val2:
                        is_contradiction = True
                    elif op1 == "GTE" and op2 == "LT" and val1 >= val2:
                        is_contradiction = True
                    elif op1 == "GTE" and op2 == "LTE" and val1 > val2:
                        is_contradiction = True

                elif op2 in ("GT", "GTE") and op1 in ("LT", "LTE"):
                    if op2 == "GT" and op1 in ("LT", "LTE") and val2 >= val1:
                        is_contradiction = True
                    elif op2 == "GTE" and op1 == "LT" and val2 >= val1:
                        is_contradiction = True
                    elif op2 == "GTE" and op1 == "LTE" and val2 > val1:
                        is_contradiction = True

                # Case 2: EQ vs anything
                elif op1 == "EQ" and op2 == "EQ" and val1 != val2:
                    is_contradiction = True
                elif op1 == "EQ":
                    if op2 == "GT" and val1 <= val2:
                        is_contradiction = True
                    elif op2 == "GTE" and val1 < val2:
                        is_contradiction = True
                    elif op2 == "LT" and val1 >= val2:
                        is_contradiction = True
                    elif op2 == "LTE" and val1 > val2:
                        is_contradiction = True
                elif op2 == "EQ":
                    if op1 == "GT" and val2 <= val1:
                        is_contradiction = True
                    elif op1 == "GTE" and val2 < val1:
                        is_contradiction = True
                    elif op1 == "LT" and val2 >= val1:
                        is_contradiction = True
                    elif op1 == "LTE" and val2 > val1:
                        is_contradiction = True

                if is_contradiction:
                    warnings.append(
                        f"Contradictory conditions on '{m}': cannot simultaneously satisfy {op1} {val1} and {op2} {val2}."
                    )

    return warnings


def check_rule_overlaps(
    active_rules: list[Any],
    draft_param: str,
    draft_scope_mode: str,
    draft_campaign_ids: list[int],
    exclude_rule_id: int | None = None,
) -> list[str]:
    """
    Checks for overlapping active rules targeting the same parameter and campaigns (Q14).
    """
    warnings: list[str] = []
    draft_camps = set(draft_campaign_ids)

    for rule in active_rules:
        rid = getattr(rule, "id", None)
        if exclude_rule_id and rid == exclude_rule_id:
            continue
        if not getattr(rule, "is_active", True):
            continue
        if getattr(rule, "affected_parameter", "") != draft_param:
            continue

        rule_scope = getattr(rule, "scope_mode", "ALL")
        rule_camps = set(getattr(rule, "campaign_ids", []) or [])

        # Check scope overlap
        scopes_overlap = False
        if draft_scope_mode == "ALL" or rule_scope == "ALL":
            scopes_overlap = True
        elif draft_scope_mode == "INCLUDE" and rule_scope == "INCLUDE":
            if draft_camps & rule_camps:
                scopes_overlap = True
        elif draft_scope_mode == "INCLUDE" and rule_scope == "EXCLUDE":
            # Overlaps if any included campaign is not excluded
            if draft_camps - rule_camps:
                scopes_overlap = True
        elif draft_scope_mode == "EXCLUDE" and rule_scope == "INCLUDE":
            if rule_camps - draft_camps:
                scopes_overlap = True
        elif draft_scope_mode == "EXCLUDE" and rule_scope == "EXCLUDE":
            # Both exclude some campaigns, meaning all others overlap
            scopes_overlap = True

        if scopes_overlap:
            rule_name = getattr(rule, "name", f"Rule #{rid}")
            warnings.append(
                f"Overlap warning: Active rule '{rule_name}' also targets {draft_param} with overlapping campaign scope."
            )

    return warnings
