"""Pace: offensive plays per minute of possession, computed at the DRIVE level.

Play-by-play clocks from ESPN (and therefore CFBD) are stale for long stretches
in roughly half of all games, so per-play clock deltas are unusable. Drive start
and end clocks are reliable, so:

    plays_per_min = sum(scrimmage plays on qualifying drives)
                    / (sum(elapsed seconds of those drives) / 60)
    sec_per_play  = 60 / plays_per_min

Qualifying (situation-neutral) drive:
  * elapsed > 0 and at least one scrimmage play
  * not garbage time at the drive start (same quarter thresholds as filters.py,
    applied to the score margin when the drive began)
  * does not start with <= 2:00 left in Q2 or Q4 (config metrics.two_minute_filter)
  * driveResult is not END OF HALF / END OF GAME (kneel-out drives)
Play counts come from the play feed (scrimmage plays only, kneels and spikes and
no-play penalties removed), not from the drive summary, so kneels never count
as plays.

All-situations plays/min (raw tab only) uses every drive with elapsed > 0.
Rank 1 = fastest, FBS only.
"""

from __future__ import annotations

import polars as pl

from cfb_dfs.config import MetricsConfig
from cfb_dfs.transform.filters import garbage_time_expr
from cfb_dfs.transform.ranks import add_rank
from cfb_dfs.transform.splits import add_game_order, window_sums

KNEEL_OUT_RESULTS = {"END OF HALF", "END OF GAME", "END OF 4TH QUARTER"}


def drive_play_counts(plays: pl.DataFrame) -> pl.DataFrame:
    """Per drive: scrimmage plays excluding kneels, spikes, and no-play penalties."""
    ok = (
        pl.col("scrimmage_play").fill_null(False)
        & ~pl.col("kneel_down").fill_null(False)
        & ~pl.col("text").fill_null("").str.to_lowercase().str.contains("spike")
        & ~pl.col("penalty_no_play").fill_null(False)
    )
    return (
        plays.group_by(["game_id", "drive_id"])
        .agg(ok.sum().alias("plays"))
        .with_columns(pl.col("plays").cast(pl.Int64))
    )


def qualify_drives(
    drives: pl.DataFrame, plays: pl.DataFrame, metrics: MetricsConfig
) -> pl.DataFrame:
    df = drives.join(drive_play_counts(plays), on=["game_id", "drive_id"], how="left").with_columns(
        pl.col("plays").fill_null(0)
    )
    garbage = garbage_time_expr(metrics.garbage_time, period="start_period", margin="start_margin")
    two_min = pl.col("start_period").is_in([2, 4]) & (pl.col("start_secs") <= 120)
    kneel_out = pl.col("result").fill_null("").str.to_uppercase().is_in(list(KNEEL_OUT_RESULTS))
    usable = (pl.col("elapsed_secs") > 0) & (pl.col("plays") > 0) & pl.col("team_id").is_not_null()
    neutral = usable & ~garbage & ~kneel_out
    if metrics.two_minute_filter:
        neutral = neutral & ~two_min
    return df.with_columns([usable.alias("usable"), neutral.alias("neutral")])


def team_game_pace(
    drives: pl.DataFrame, plays: pl.DataFrame, metrics: MetricsConfig
) -> pl.DataFrame:
    """Per (game_id, team_id): neutral and all-situation plays/seconds."""
    q = qualify_drives(drives, plays, metrics)
    return (
        q.filter(pl.col("usable"))
        .group_by(["game_id", "team_id"])
        .agg(
            [
                pl.when(pl.col("neutral"))
                .then(pl.col("plays"))
                .otherwise(0)
                .sum()
                .alias("neutral_plays"),
                pl.when(pl.col("neutral"))
                .then(pl.col("elapsed_secs"))
                .otherwise(0)
                .sum()
                .alias("neutral_secs"),
                pl.col("plays").sum().alias("all_plays"),
                pl.col("elapsed_secs").sum().alias("all_secs"),
                pl.len().alias("drives"),
            ]
        )
        .sort(["game_id", "team_id"])
    )


def team_pace(
    drives: pl.DataFrame,
    plays: pl.DataFrame,
    metrics: MetricsConfig,
    game_order: dict[int, int],
    fbs_ids: set[int],
    window: int = 3,
) -> pl.DataFrame:
    """One row per team with season + last-N pace and FBS rank (1 = fastest)."""
    tg = add_game_order(team_game_pace(drives, plays, metrics), game_order)
    w = window_sums(
        tg, ["neutral_plays", "neutral_secs", "all_plays", "all_secs", "drives"], window
    )
    lw = f"_l{window}"
    df = w.with_columns(
        [
            _ppm("neutral_plays", "neutral_secs", "plays_per_min"),
            _ppm(f"neutral_plays{lw}", f"neutral_secs{lw}", f"plays_per_min{lw}"),
            _ppm("all_plays", "all_secs", "all_plays_per_min"),
        ]
    ).with_columns(
        pl.when(pl.col("plays_per_min") > 0)
        .then(60 / pl.col("plays_per_min"))
        .otherwise(None)
        .round(2)
        .alias("sec_per_play")
    )
    df = add_rank(df, "plays_per_min", "pace_rank", descending=True, population=fbs_ids)
    df = add_rank(df, f"plays_per_min{lw}", f"pace_rank{lw}", descending=True, population=fbs_ids)
    return df.sort("pace_rank", nulls_last=True)


def _ppm(plays: str, secs: str, alias: str) -> pl.Expr:
    return (
        pl.when(pl.col(secs) > 0)
        .then(pl.col(plays) / (pl.col(secs) / 60))
        .otherwise(None)
        .round(3)
        .alias(alias)
    )
