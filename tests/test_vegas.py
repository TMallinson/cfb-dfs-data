from __future__ import annotations

from cfb_dfs.models import BookLine, GameLines
from cfb_dfs.transform.vegas import all_team_lines, choose_book, implied_total, team_lines

PREF = ["DraftKings", "Bovada", "consensus"]


def _game(lines: list[BookLine]) -> GameLines:
    return GameLines.model_validate(
        {
            "id": 1,
            "season": 2026,
            "week": 2,
            "seasonType": "regular",
            "startDate": "2026-09-12T16:00:00.000Z",
            "homeTeamId": 10,
            "homeTeam": "Home",
            "awayTeamId": 20,
            "awayTeam": "Away",
            "lines": [ln.model_dump(by_alias=True) for ln in lines],
        }
    )


def test_implied_totals_reconcile_and_follow_sign_convention():
    # Home favored by 5.5 (spread -5.5 home perspective), total 50.5
    g = _game([BookLine(provider="DraftKings", spread=-5.5, overUnder=50.5)])
    home, away = team_lines(g, PREF)
    assert home.spread == -5.5 and away.spread == 5.5
    assert home.implied_total == 28.0 and away.implied_total == 22.5
    assert home.implied_total + away.implied_total == 50.5
    assert home.book == "DraftKings"


def test_book_preference_and_fallback():
    lines = [
        BookLine(provider="Bovada", spread=3, overUnder=44),
        BookLine(provider="DraftKings", spread=None, overUnder=45),
    ]
    # DK lacks a spread -> Bovada is chosen even though DK is preferred
    assert choose_book(lines, PREF).provider == "Bovada"
    assert choose_book([BookLine(provider="X", overUnder=40)], PREF).provider == "X"
    assert choose_book([], PREF) is None


def test_missing_lines_yield_blank_but_present_rows():
    g = _game([])
    home, _away = team_lines(g, PREF)
    assert home.total is None and home.implied_total is None and home.book is None
    assert home.books_available == "none"
    assert implied_total(None, 3) is None


def test_real_fixture_rows(load_json):
    games = [GameLines.model_validate(r) for r in load_json("cfbd_lines_2026_w2.json")]
    out = all_team_lines(games, PREF)
    for pair in out.values():
        h, a = pair
        if h.total is not None:
            assert abs(h.implied_total + a.implied_total - h.total) < 1e-9
            assert h.spread == -a.spread
