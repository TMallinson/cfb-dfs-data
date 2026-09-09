"""Week detection from the CFBD calendar, with a date-based fallback."""

from __future__ import annotations

from datetime import datetime, timedelta

from cfb_dfs.models import CalendarWeek


def resolve_week(
    calendar: list[CalendarWeek], now: datetime, season_type: str = "regular"
) -> int | None:
    """Return the calendar week containing `now` (tz-aware).

    CFBD weeks run Monday 07:00 UTC to the following Monday 06:59 UTC, so from
    Monday morning (02:00 CT) the *upcoming* slate is the detected week. If
    `now` is before the first week, week 1; after the last, the last week.
    """
    weeks = sorted((w for w in calendar if w.season_type == season_type), key=lambda w: w.week)
    if not weeks:
        return None
    for w in weeks:
        if w.start_date <= now <= w.end_date:
            return w.week
    if now < weeks[0].start_date:
        return weeks[0].week
    return weeks[-1].week


def fallback_week(now: datetime, season: int) -> int:
    """No calendar available: week 1 is the Labor Day weekend (Saturday before the
    first Monday of September); earlier games (a "week 0") also count as week 1."""
    sep1 = datetime(season, 9, 1, tzinfo=now.tzinfo)
    labor_day = sep1 + timedelta(days=(7 - sep1.weekday()) % 7)
    week1_monday = labor_day - timedelta(days=7)
    delta_days = (now - week1_monday).days
    return max(1, delta_days // 7 + 1)
