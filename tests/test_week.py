from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from cfb_dfs.models import CalendarWeek
from cfb_dfs.week import fallback_week, resolve_week

CT = ZoneInfo("America/Chicago")


def _calendar(load_json) -> list[CalendarWeek]:
    return [CalendarWeek.model_validate(r) for r in load_json("cfbd_calendar_2026.json")]


def test_calendar_parses(load_json):
    cal = _calendar(load_json)
    assert len(cal) == 16
    regular = [w for w in cal if w.season_type == "regular"]
    assert [w.week for w in regular] == list(range(1, 16))


def test_week_detected_midweek(load_json):
    cal = _calendar(load_json)
    assert resolve_week(cal, datetime(2026, 9, 9, 15, 0, tzinfo=CT)) == 2


def test_week_flips_monday_morning(load_json):
    cal = _calendar(load_json)
    # Sunday night after week-2 games still week 2; Monday 06:00 CT is week 3.
    assert resolve_week(cal, datetime(2026, 9, 13, 23, 0, tzinfo=CT)) == 2
    assert resolve_week(cal, datetime(2026, 9, 14, 6, 0, tzinfo=CT)) == 3


def test_week_before_and_after_season(load_json):
    cal = _calendar(load_json)
    assert resolve_week(cal, datetime(2026, 8, 1, tzinfo=CT)) == 1
    assert resolve_week(cal, datetime(2026, 12, 20, tzinfo=CT)) == 15


def test_fallback_week_matches_calendar(load_json):
    cal = _calendar(load_json)
    days = (
        datetime(2026, 8, 25),
        datetime(2026, 9, 9),
        datetime(2026, 9, 16),
        datetime(2026, 10, 7),
    )
    for day in days:
        d = day.replace(tzinfo=CT)
        assert fallback_week(d, 2026) == resolve_week(cal, d)
