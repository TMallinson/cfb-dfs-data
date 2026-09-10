from __future__ import annotations

import polars as pl
import pytest

from cfb_dfs.models import Team
from cfb_dfs.sheets.format import FLIPPED_RANK_AFTER, conditional_format_requests, gradient
from cfb_dfs.sheets.layout import MAIN_HEADER
from cfb_dfs.transform.players import (
    passer_games,
    passing_frame,
    roster_index,
    rusher_games,
    rushing_frame,
    season_table,
    target_games,
    team_game_counts,
)
from cfb_dfs.transform.teams import TeamIndex

ORDER = {401856634: 0, 401856777: 1, 401858423: 2}


@pytest.fixture(scope="module")
def frames(load_json):
    teams = TeamIndex([Team.model_validate(t) for t in load_json("cfbd_teams_subset.json")])
    passing = passing_frame(load_json("cfbd_passing_plays_w1_sample.json"), teams)
    rushing = rushing_frame(load_json("cfbd_rushing_plays_w1_sample.json"), teams)
    roster = roster_index(load_json("cfbd_roster_sample.json"))
    return teams, passing, rushing, roster


def test_frames_parse(frames):
    _, passing, rushing, roster = frames
    assert passing.height > 100 and rushing.height > 150
    assert passing["team_id"].null_count() == 0 and rushing["team_id"].null_count() == 0
    assert passing.filter(pl.col("is_spike")).height == 0
    assert roster and all("position" in v for v in roster.values())


def test_target_and_passer_games(frames):
    _, passing, rushing, roster = frames
    tg = target_games(passing)
    assert tg["targets"].sum() == passing.filter(pl.col("target_id").is_not_null()).height
    assert tg.filter(pl.col("receptions") > pl.col("targets")).height == 0
    pg = passer_games(passing, rushing)
    assert pg["attempts"].sum() == passing.height
    assert pg.filter(pl.col("dropbacks") < pl.col("attempts")).height == 0
    top = pg.sort("attempts", descending=True).row(0, named=True)
    assert roster.get(top["player_id"], {}).get("position") == "QB"


def test_rusher_games_exclude_sacks_and_kneels(frames):
    _, passing, rushing, _ = frames
    rg = rusher_games(rushing, passing)
    n_valid = rushing.filter(
        ~pl.col("is_sack")
        & ~pl.col("is_kneel")
        & ~pl.col("is_team")
        & (pl.col("attribution") == "individual")
        & pl.col("rusher_id").is_not_null()
    ).height
    assert rg["carries"].sum() == n_valid
    assert rg.filter(pl.col("targets") > 0).height > 0  # RBs also catch passes


def test_season_table_weekly_and_windows(frames):
    _, passing, rushing, _ = frames
    tg = target_games(passing)
    team_games = team_game_counts([passing, rushing])
    assert set(team_games.values()) == {1}
    st = season_table(tg, "targets", ["targets", "receptions"], ORDER, team_games, window=3)
    assert st.height == tg["player_id"].n_unique()
    r = st.sort("targets", descending=True).row(0, named=True)
    assert r["wk1"] == r["targets"] and r["targets_l3"] == r["targets"] and r["games"] == 1
    assert r["team_games"] == 1 and r["wk2"] is None


def test_formatting_rules_flip_rroe_rank_and_use_percentiles():
    reqs = conditional_format_requests(1, MAIN_HEADER, 2, 0)
    adds = [r["addConditionalFormatRule"]["rule"] for r in reqs if "addConditionalFormatRule" in r]
    by_col = {r["ranges"][0]["startColumnIndex"]: r["gradientRule"] for r in adds}
    rroe_rk = MAIN_HEADER.index("RROE%") + 1
    pace_rk = MAIN_HEADER.index("Pace Rk")
    assert MAIN_HEADER[rroe_rk] == "Rk" and "RROE%" in FLIPPED_RANK_AFTER
    assert by_col[rroe_rk]["maxpoint"]["color"] == gradient("high_good")["maxpoint"]["color"]
    assert by_col[pace_rk]["minpoint"]["color"] == gradient("low_good")["minpoint"]["color"]
    assert by_col[MAIN_HEADER.index("Total")]["minpoint"]["type"] == "PERCENTILE"
    wind = by_col[MAIN_HEADER.index("Wind mph")]
    assert wind["midpoint"] == {
        "color": gradient("wind")["midpoint"]["color"],
        "type": "NUMBER",
        "value": "15",
    }
    temp = by_col[MAIN_HEADER.index("Temp °F")]
    assert temp["minpoint"]["value"] == "35" and temp["maxpoint"]["value"] == "95"
