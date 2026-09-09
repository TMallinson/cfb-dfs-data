from __future__ import annotations

from cfb_dfs.models import GameLines


def test_lines_fixture_parses_and_sign_convention(load_json):
    rows = [GameLines.model_validate(r) for r in load_json("cfbd_lines_2026_w2.json")]
    assert rows
    mizzou_ku = next(r for r in rows if r.home_team == "Kansas" and r.away_team == "Missouri")
    dk = next(line for line in mizzou_ku.lines if line.provider == "DraftKings")
    # "Missouri -5.5" with Missouri away => home-perspective spread is +5.5
    assert dk.formatted_spread == "Missouri -5.5"
    assert dk.spread == 5.5
    assert dk.over_under == 50.5
    assert mizzou_ku.id == 401856678  # same id space as ESPN


def test_lines_tolerate_missing_books(load_json):
    rows = [GameLines.model_validate(r) for r in load_json("cfbd_lines_2026_w2.json")]
    assert all(isinstance(r.lines, list) for r in rows)
