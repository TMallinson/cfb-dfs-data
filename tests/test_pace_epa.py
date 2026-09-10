from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from cfb_dfs.config import GarbageTime, MetricsConfig
from cfb_dfs.models import Team
from cfb_dfs.transform.epa import team_epa, team_game_epa
from cfb_dfs.transform.filters import describe_filters, prepare_plays
from cfb_dfs.transform.pace import drive_play_counts, qualify_drives, team_game_pace, team_pace
from cfb_dfs.transform.plays import normalize_drives, normalize_plays
from cfb_dfs.transform.ranks import add_rank
from cfb_dfs.transform.teams import TeamIndex

FIX = Path(__file__).parent / "fixtures"
ALABAMA, ECU, BC, CIN, UMASS, RUTGERS = 333, 151, 103, 2132, 113, 164
FBS = {ALABAMA, ECU, BC, CIN, UMASS, RUTGERS}
ORDER = {401856634: 0, 401856777: 1, 401858423: 2}


@pytest.fixture(scope="module")
def teams(load_json) -> TeamIndex:
    return TeamIndex([Team.model_validate(t) for t in load_json("cfbd_teams_subset.json")])


@pytest.fixture(scope="module")
def raw(load_json, teams) -> pl.DataFrame:
    return normalize_plays(load_json("cfbd_plays_2026_w1_sample.json"), teams, week=1)


@pytest.fixture(scope="module")
def drives(load_json, teams) -> pl.DataFrame:
    return normalize_drives(load_json("cfbd_drives_2026_w1_sample.json"), teams)


@pytest.fixture(scope="module")
def plays(raw: pl.DataFrame) -> pl.DataFrame:
    return prepare_plays(raw, MetricsConfig())


def test_normalize_plays_shapes(raw: pl.DataFrame):
    assert raw.height == 513 and raw["game_id"].n_unique() == 3
    assert raw["pos_team_id"].null_count() == 0
    assert set(raw.filter(pl.col("sack"))["type.text"].unique().to_list()) == {"Sack"}
    assert raw.filter(pl.col("pass") & pl.col("rush")).height == 0
    assert raw.filter(pl.col("scrimmage_play"))["EPA"].null_count() < 5
    assert raw.filter(pl.col("kneel_down")).height > 0
    # game_play_number is a dense 0..n-1 ordinal per game
    g = raw.filter(pl.col("game_id") == 401856634)
    assert g["game_play_number"].to_list() == list(range(g.height))


def test_prepare_flags_are_consistent(plays: pl.DataFrame):
    assert plays.filter(pl.col("is_dropback") & pl.col("is_rush")).height == 0
    assert plays.filter(pl.col("is_kneel") & pl.col("epa_eligible")).height == 0
    assert plays.filter(pl.col("is_garbage") & pl.col("epa_eligible")).height == 0
    assert plays.filter(pl.col("is_two_minute") & pl.col("pace_eligible")).height == 0
    assert plays.filter(pl.col("is_sack") & ~pl.col("is_dropback")).height == 0
    assert plays.filter(~pl.col("is_scrimmage") & pl.col("epa_eligible")).height == 0


def test_garbage_time_thresholds(raw: pl.DataFrame):
    strict = prepare_plays(raw, MetricsConfig(garbage_time=GarbageTime(q1=0, q2=0, q3=0, q4=0)))
    loose = prepare_plays(
        raw, MetricsConfig(garbage_time=GarbageTime(q1=None, q2=None, q3=None, q4=None))
    )
    assert strict["is_garbage"].sum() > loose["is_garbage"].sum() == 0
    assert "garbage time" in describe_filters(MetricsConfig())


def test_drive_qualification(drives: pl.DataFrame, raw: pl.DataFrame):
    counts = drive_play_counts(raw)
    assert counts["plays"].sum() < raw.filter(pl.col("scrimmage_play")).height  # kneels removed
    q = qualify_drives(drives, raw, MetricsConfig())
    assert q.filter(pl.col("neutral") & ~pl.col("usable")).height == 0
    kneel_out = q.filter(pl.col("result").str.to_uppercase().is_in(["END OF HALF", "END OF GAME"]))
    assert kneel_out.height > 0 and kneel_out["neutral"].sum() == 0
    assert q.filter(pl.col("usable"))["elapsed_secs"].min() > 0


def test_team_pace_ranks_fbs_only(drives: pl.DataFrame, plays: pl.DataFrame):
    df = team_pace(drives, plays, MetricsConfig(), ORDER, FBS, window=3)
    assert set(df["team_id"].to_list()) == FBS
    ranks = df.filter(pl.col("pace_rank").is_not_null()).sort("pace_rank")
    assert ranks["pace_rank"].to_list() == [1, 2, 3, 4, 5, 6]
    fastest = ranks.row(0, named=True)
    assert fastest["plays_per_min"] == ranks["plays_per_min"].max()
    assert abs(fastest["sec_per_play"] - 60 / fastest["plays_per_min"]) < 0.02
    # Sanity range for college pace: roughly 1.8 - 3.5 plays per possession minute
    assert ranks["plays_per_min"].min() > 1.5 and ranks["plays_per_min"].max() < 4.0
    assert df.filter(pl.col("plays_per_min") != pl.col("plays_per_min_l3")).height == 0
    tg = team_game_pace(drives, plays, MetricsConfig())
    assert tg.filter(pl.col("neutral_plays") > pl.col("all_plays")).height == 0
    # Boston College's game had 15 plays in the ESPN feed; CFBD has the full game
    bc = tg.filter(pl.col("team_id") == BC).row(0, named=True)
    assert bc["all_plays"] > 40


def test_team_epa_offense_and_defense_mirror(plays: pl.DataFrame):
    tg = team_game_epa(plays)
    g = tg.filter(pl.col("game_id") == 401856634)
    off_bama = g.filter((pl.col("team_id") == ALABAMA) & (pl.col("side") == "off")).row(
        0, named=True
    )
    def_ecu = g.filter((pl.col("team_id") == ECU) & (pl.col("side") == "def")).row(0, named=True)
    assert (
        off_bama["db_n"] == def_ecu["db_n"] and abs(off_bama["db_epa"] - def_ecu["db_epa"]) < 1e-9
    )
    assert off_bama["db_n"] > 15 and off_bama["rush_n"] > 15

    df = team_epa(plays, ORDER, FBS, window=3)
    r = df.filter(pl.col("off_epa_db_rank").is_not_null()).sort("off_epa_db_rank")
    assert r["off_epa_db_rank"].to_list() == [1, 2, 3, 4, 5, 6]
    assert r["off_epa_db"].to_list() == sorted(r["off_epa_db"].to_list(), reverse=True)
    d = df.filter(pl.col("def_epa_db_rank").is_not_null()).sort("def_epa_db_rank")
    assert d["def_epa_db"].to_list() == sorted(d["def_epa_db"].to_list())  # 1 = fewest allowed
    assert r["off_epa_db"].min() > -1.5 and r["off_epa_db"].max() < 1.5


def test_qb_rush_flag_moves_plays_between_buckets(raw: pl.DataFrame):
    # CFBD plays carry no passer names, so the flag is a no-op there (documented).
    a = prepare_plays(raw, MetricsConfig(include_scrambles_in_dropback=False))
    b = prepare_plays(raw, MetricsConfig(include_scrambles_in_dropback=True))
    assert (
        a["is_dropback"].sum() + a["is_rush"].sum() == b["is_dropback"].sum() + b["is_rush"].sum()
    )


def test_add_rank_ties_share_min_rank():
    df = pl.DataFrame({"team_id": [1, 2, 3, 4], "v": [3.0, 3.0, 1.0, 9.0]})
    out = add_rank(df, "v", "rk", descending=True, population={1, 2, 3}).sort("team_id")
    assert out["rk"].to_list() == [1, 1, 3, None]
