"""End-to-end over cached fixtures: shape and sanity of every table the sheet gets."""

from __future__ import annotations

import polars as pl
import pytest

from cfb_dfs.config import MetricsConfig
from cfb_dfs.models import Game, GameLines, Team
from cfb_dfs.sheets.format import (
    PASSING_RULES,
    RUSHING_RULES,
    TARGET_RULES,
    conditional_format_requests,
)
from cfb_dfs.sheets.layout import PASSING_RAW_HEADER, RUSHING_RAW_HEADER, TARGETS_RAW_HEADER
from cfb_dfs.transform.epa import team_epa
from cfb_dfs.transform.filters import prepare_plays
from cfb_dfs.transform.pace import team_pace
from cfb_dfs.transform.players import (
    passer_games,
    passing_frame,
    rusher_games,
    rushing_frame,
    season_table,
    target_games,
    team_game_counts,
)
from cfb_dfs.transform.plays import normalize_drives, normalize_plays
from cfb_dfs.transform.teams import TeamIndex
from cfb_dfs.transform.vegas import all_team_lines

ORDER = {401856634: 0, 401856777: 1, 401858423: 2}


@pytest.fixture(scope="module")
def world(load_json):
    teams = TeamIndex([Team.model_validate(t) for t in load_json("cfbd_teams_subset.json")])
    plays = prepare_plays(
        normalize_plays(load_json("cfbd_plays_2026_w1_sample.json"), teams, 1), MetricsConfig()
    )
    drives = normalize_drives(load_json("cfbd_drives_2026_w1_sample.json"), teams)
    passing = passing_frame(load_json("cfbd_passing_plays_w1_sample.json"), teams)
    rushing = rushing_frame(load_json("cfbd_rushing_plays_w1_sample.json"), teams)
    lines = [GameLines.model_validate(r) for r in load_json("cfbd_lines_2026_w2.json")]
    games = [Game.model_validate(g) for g in load_json("cfbd_games_2026_w2_subset.json")]
    return teams, plays, drives, passing, rushing, lines, games


def _ranks_are_dense(df: pl.DataFrame, col: str) -> None:
    ranks = sorted(df[col].drop_nulls().to_list())
    assert ranks == list(range(1, len(ranks) + 1)), f"{col} ranks have gaps: {ranks}"


def test_vegas_reconciles_and_has_no_null_keys(world):
    _, _, _, _, _, lines, games = world
    tl = all_team_lines(lines, ["DraftKings", "Bovada"])
    assert all(g.id in tl for g in games if g.id in {ln.id for ln in lines})
    for home, away in tl.values():
        assert home.team_id and away.team_id and home.game_id == away.game_id
        if home.total is not None:
            assert abs(home.implied_total + away.implied_total - home.total) < 1e-9
            assert home.spread == -away.spread


def test_team_metrics_shape(world):
    teams, plays, drives, *_ = world
    fbs = teams.fbs_ids()
    pace = team_pace(drives, plays, MetricsConfig(), ORDER, fbs)
    epa = team_epa(plays, ORDER, fbs)
    for df, key_cols in (
        (pace, ["plays_per_min", "games"]),
        (epa, ["off_epa_db", "def_epa_db", "games"]),
    ):
        assert df["team_id"].null_count() == 0 and df["team_id"].n_unique() == df.height
        for c in key_cols:
            assert df.filter(pl.col("team_id").is_in(list(fbs)))[c].null_count() == 0
    _ranks_are_dense(pace, "pace_rank")
    for c in ("off_epa_db_rank", "off_epa_rush_rank", "def_epa_db_rank", "def_epa_rush_rank"):
        _ranks_are_dense(epa, c)


def test_player_tables_match_headers_and_are_sane(world):
    _, _, _, passing, rushing, _, _ = world
    tg = target_games(passing, rushing)
    pg = passer_games(passing, rushing)
    rg = rusher_games(rushing, passing)
    team_games = team_game_counts([passing, rushing])
    t = season_table(tg, "targets", ["targets", "receptions", "team_db"], ORDER, team_games)
    p = season_table(pg, "attempts", ["attempts", "dropbacks"], ORDER, team_games)
    r = season_table(rg, "carries", ["carries", "rush_yards"], ORDER, team_games)
    for df, vol in ((t, "targets"), (p, "attempts"), (r, "carries")):
        assert df["player_id"].n_unique() == df.height and df[vol].min() >= 1
        assert df["team_id"].null_count() == 0
    assert (t["targets"] <= t["team_db"]).all()
    # Targets per team dropback is a share, so it must sit in (0, 1].
    share = (t["targets"] / t["team_db"]).drop_nulls()
    assert share.min() > 0 and share.max() <= 1
    # Weekly column count matches the sheet header layout.
    assert TARGETS_RAW_HEADER[-16:] == [f"Wk{w}" for w in range(1, 17)]
    assert PASSING_RAW_HEADER[-16:] == RUSHING_RAW_HEADER[-16:] == TARGETS_RAW_HEADER[-16:]


def test_display_tab_rules_reference_real_headers():
    for header, rules in (
        (TARGETS_RAW_HEADER, TARGET_RULES),
        (PASSING_RAW_HEADER, PASSING_RULES),
        (RUSHING_RAW_HEADER, RUSHING_RULES),
    ):
        missing = [r.header for r in rules if r.header not in header]
        assert not missing, missing
        reqs = conditional_format_requests(3, header, 3, 2, rules=rules, week_block=True)
        adds = [x for x in reqs if "addConditionalFormatRule" in x]
        block = adds[-1]["addConditionalFormatRule"]["rule"]["ranges"][0]
        assert block["endColumnIndex"] - block["startColumnIndex"] == 16
        assert len(adds) == len(rules) + 1
