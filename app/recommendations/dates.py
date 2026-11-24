from datetime import date, timedelta
from app.recommendations.registry import DATE_FILTERS

CONVERSION_METRICS = {
    "orders",
    "sales",
    "acos",
    "roas",
    "cvr",
    "acos_vs_target",
    "target_sales_share",
    "orders_change_pct",
    "sales_change_pct",
    "acos_change_pct",
    "cvr_change_pct",
}


def resolve_date_filter(
    key: str,
    today: date,
    has_conversion_metrics: bool | None = None,
) -> tuple[date, date]:
    """
    Resolve date filter key to (start_date, end_date) inclusive.

    If has_conversion_metrics is True:
        Window ends at T-2 (2 days ago), accounting for Amazon attribution lag (Q3, F9).
    If has_conversion_metrics is False:
        Window ends at T-1 (yesterday), evaluating completed traffic.
    If has_conversion_metrics is None:
        Raw calendar boundary ending on `today` (for backwards compatibility).
    """
    if key not in DATE_FILTERS:
        raise ValueError(f"Unknown date filter: {key}")

    if has_conversion_metrics is None:
        delay = 0
    elif has_conversion_metrics:
        delay = 2
    else:
        delay = 1

    effective_today = today - timedelta(days=delay)

    if key == "TODAY":
        return effective_today, effective_today
    elif key == "YESTERDAY":
        y = today - timedelta(days=max(1, delay))
        return y, y
    elif key == "LAST_7_DAYS":
        return effective_today - timedelta(days=6), effective_today
    elif key == "THIS_WEEK":
        start = effective_today - timedelta(days=effective_today.weekday())
        return min(start, effective_today), effective_today
    elif key == "LAST_WEEK":
        this_monday = today - timedelta(days=today.weekday())
        start = this_monday - timedelta(days=7)
        end = this_monday - timedelta(days=1)
        if end > effective_today:
            end = effective_today
        return min(start, end), end
    elif key == "LAST_30_DAYS":
        return effective_today - timedelta(days=29), effective_today
    elif key == "THIS_MONTH":
        start = date(effective_today.year, effective_today.month, 1)
        return min(start, effective_today), effective_today
    elif key == "LAST_MONTH":
        if today.month == 1:
            prev_year = today.year - 1
            prev_month = 12
        else:
            prev_year = today.year
            prev_month = today.month - 1
        start = date(prev_year, prev_month, 1)
        end = date(today.year, today.month, 1) - timedelta(days=1)
        if end > effective_today:
            end = effective_today
        return min(start, end), end
    elif key == "YEAR_TO_DATE":
        start = date(effective_today.year, 1, 1)
        return min(start, effective_today), effective_today
    elif key == "LIFETIME":
        return date(2000, 1, 1), effective_today

    raise ValueError(f"Unhandled date filter: {key}")


def parse_freeze_periods(freeze_str: str | None) -> list[tuple[date, date]]:
    """
    Parses comma-separated date ranges in YYYY-MM-DD:YYYY-MM-DD format (Q12).
    E.g.: '2026-11-20:2026-11-25, 2026-12-24:2026-12-26'
    """
    if not freeze_str or not freeze_str.strip():
        return []
    periods: list[tuple[date, date]] = []
    for item in freeze_str.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        if len(parts) == 2:
            try:
                start_d = date.fromisoformat(parts[0].strip())
                end_d = date.fromisoformat(parts[1].strip())
                if start_d <= end_d:
                    periods.append((start_d, end_d))
                else:
                    periods.append((end_d, start_d))
            except ValueError:
                continue
    return periods


def is_in_freeze_period(
    check_date: date,
    freeze_str: str | None = None,
) -> tuple[bool, str | None]:
    """
    Checks if check_date falls within any configured freeze period (Q12).
    Returns (True, f"{start} to {end}") if within a freeze period; otherwise (False, None).
    """
    from app.config import settings
    raw_str = freeze_str if freeze_str is not None else getattr(settings, "FREEZE_PERIODS", "")
    periods = parse_freeze_periods(raw_str)
    for start, end in periods:
        if start <= check_date <= end:
            return True, f"{start.isoformat()} to {end.isoformat()}"
    return False, None
