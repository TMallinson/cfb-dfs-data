"""EPA per play splits, offense and defense.

Buckets (see filters.py for the exact flags):
  dropback = pass attempts + sacks + interceptions/throwaways (+ QB rushes if configured)
  rush     = designed rushes (not dropback, not kneel)
Population: `epa_eligible` plays (scrimmage, no kneel/spike/no-play, no garbage time).
Offense value for team T = mean EPA over T's offensive plays in the bucket;
defense value = mean EPA over plays where T is the defense (EPA is always from the
offense's perspective, so lower = better defense).
Ranks: offense 1 = highest EPA; defense 1 = lowest EPA allowed. FBS only.
"""

from __future__ import annotations

import polars as pl

from cfb_dfs.transform.ranks import add_rank
from cfb_dfs.transform.splits import add_game_order, ratio, window_sums

SUM_COLS = ["db_n", "db_epa", "rush_n", "rush_epa", "play_n", "play_epa"]


def team_game_epa(plays: pl.DataFrame) -> pl.DataFrame:
    """Per (game_id, team_id, side): counts and EPA sums for each bucket."""
    df = plays.filter(pl.col("epa_eligible") & pl.col("EPA").is_not_null())
    aggs = [
        pl.col("is_dropback").sum().alias("db_n"),
        pl.when(pl.col("is_dropback")).then(pl.col("EPA")).otherwise(0).sum().alias("db_epa"),
        pl.col("is_rush").sum().alias("rush_n"),
        pl.when(pl.col("is_rush")).then(pl.col("EPA")).otherwise(0).sum().alias("rush_epa"),
        (pl.col("is_dropback") | pl.col("is_rush")).sum().alias("play_n"),
        pl.when(pl.col("is_dropback") | pl.col("is_rush"))
        .then(pl.col("EPA"))
        .otherwise(0)
        .sum()
        .alias("play_epa"),
    ]
    off = (
        df.group_by(["game_id", pl.col("pos_team_id").alias("team_id")])
        .agg(aggs)
        .with_columns(pl.lit("off").alias("side"))
    )
    de = (
        df.group_by(["game_id", pl.col("def_pos_team_id").alias("team_id")])
        .agg(aggs)
        .with_columns(pl.lit("def").alias("side"))
    )
    return pl.concat([off, de]).sort(["game_id", "team_id", "side"])


def team_epa(
    plays: pl.DataFrame, game_order: dict[int, int], fbs_ids: set[int], window: int = 3
) -> pl.DataFrame:
    """One row per team: off/def EPA per dropback / rush / play, season + last-N, ranks."""
    tg = add_game_order(team_game_epa(plays), game_order)
    lw = f"_l{window}"
    sides = []
    for side in ("off", "def"):
        w = window_sums(tg.filter(pl.col("side") == side), SUM_COLS, window)
        w = w.with_columns(
            [
                ratio("db_epa", "db_n", f"{side}_epa_db"),
                ratio("rush_epa", "rush_n", f"{side}_epa_rush"),
                ratio("play_epa", "play_n", f"{side}_epa_play"),
                ratio(f"db_epa{lw}", f"db_n{lw}", f"{side}_epa_db{lw}"),
                ratio(f"rush_epa{lw}", f"rush_n{lw}", f"{side}_epa_rush{lw}"),
                pl.col("db_n").alias(f"{side}_db_n"),
                pl.col("rush_n").alias(f"{side}_rush_n"),
                pl.col("games").alias(f"{side}_games"),
            ]
        ).select(
            [
                "team_id",
                f"{side}_games",
                f"{side}_db_n",
                f"{side}_rush_n",
                f"{side}_epa_db",
                f"{side}_epa_rush",
                f"{side}_epa_play",
                f"{side}_epa_db{lw}",
                f"{side}_epa_rush{lw}",
            ]
        )
        sides.append(w)
    df = sides[0].join(sides[1], on="team_id", how="full", coalesce=True)
    df = df.with_columns(pl.max_horizontal("off_games", "def_games").alias("games"))
    for col, desc in (
        ("off_epa_db", True),
        ("off_epa_rush", True),
        ("off_epa_play", True),
        ("def_epa_db", False),
        ("def_epa_rush", False),
        ("def_epa_play", False),
        (f"off_epa_db{lw}", True),
        (f"off_epa_rush{lw}", True),
        (f"def_epa_db{lw}", False),
        (f"def_epa_rush{lw}", False),
    ):
        df = add_rank(df, col, f"{col}_rank", descending=desc, population=fbs_ids)
    return df.sort("off_epa_play_rank", nulls_last=True)
