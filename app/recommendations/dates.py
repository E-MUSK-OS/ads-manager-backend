from datetime import date, timedelta
from app.recommendations.registry import DATE_FILTERS


def resolve_date_filter(key: str, today: date) -> tuple[date, date]:
    """
    Resolve date filter key to (start_date, end_date) inclusive.
    TODAY: [today, today]
    YESTERDAY: [today-1, today-1]
    LAST_7_DAYS: [today-6, today]
    THIS_WEEK: Monday..today
    LAST_WEEK: previous Monday..Sunday
    LAST_30_DAYS: [today-29, today]
    THIS_MONTH: 1st of month..today
    LAST_MONTH: 1st of previous month..last day of previous month
    YEAR_TO_DATE: Jan 1 of year..today
    LIFETIME: [date(2000, 1, 1), today]
    """
    if key not in DATE_FILTERS:
        raise ValueError(f"Unknown date filter: {key}")

    if key == "TODAY":
        return today, today
    elif key == "YESTERDAY":
        y = today - timedelta(days=1)
        return y, y
    elif key == "LAST_7_DAYS":
        return today - timedelta(days=6), today
    elif key == "THIS_WEEK":
        start = today - timedelta(days=today.weekday())
        return start, today
    elif key == "LAST_WEEK":
        this_monday = today - timedelta(days=today.weekday())
        start = this_monday - timedelta(days=7)
        end = this_monday - timedelta(days=1)
        return start, end
    elif key == "LAST_30_DAYS":
        return today - timedelta(days=29), today
    elif key == "THIS_MONTH":
        return date(today.year, today.month, 1), today
    elif key == "LAST_MONTH":
        if today.month == 1:
            prev_year = today.year - 1
            prev_month = 12
        else:
            prev_year = today.year
            prev_month = today.month - 1
        start = date(prev_year, prev_month, 1)
        end = date(today.year, today.month, 1) - timedelta(days=1)
        return start, end
    elif key == "YEAR_TO_DATE":
        return date(today.year, 1, 1), today
    elif key == "LIFETIME":
        return date(2000, 1, 1), today

    raise ValueError(f"Unhandled date filter: {key}")
