from __future__ import annotations

from cfb_dfs.sources.espn import scoreboard_to_games_and_lines
from cfb_dfs.transform.vegas import team_lines


def test_scoreboard_parses_to_cfbd_shapes(load_json):
    games, lines = scoreboard_to_games_and_lines(load_json("espn_scoreboard_2026_w2.json"))
    assert len(games) == 3 and len(lines) == 3
    mia = next(g for g in games if g.home_team == "Miami")
    assert mia.id == 401858213 and mia.week == 2 and mia.home_id == 2390 and mia.away_id == 50
    ln = next(ln for ln in lines if ln.id == 401858213)
    dk = ln.lines[0]
    assert dk.provider == "DraftKings" and dk.spread == -57.5 and dk.over_under == 63.5
    home, away = team_lines(ln, ["DraftKings"])
    assert home.spread == -57.5 and home.implied_total == 60.5 and away.implied_total == 3.0
