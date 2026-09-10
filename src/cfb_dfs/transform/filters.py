"""Play classification and situational filters. One place, used by pace, EPA, RROE.

Definitions (also documented in README):
  scrimmage       `scrimmage_play` from the feed (excludes kickoffs, punts, FG, PAT,
                  timeouts, penalties-without-play, kneels are NOT scrimmage plays)
  kneel           `kneel_down` or play text contains "kneel"
  spike           play text contains "spike" (feed has no flag; 3 in week 1 of 2026)
  no_play         `penalty_no_play`
  garbage_time    |offense score margin at play start| > threshold for the quarter
                  (config metrics.garbage_time; default Q1 never, Q2 28, Q3 21, Q4 14;
                  overtime never)
  two_minute      period 2 or 4 with <= 2:00 on the clock
  dropback        `pass` (attempts, sacks, throwaways, interceptions) plus, when
                  metrics.include_scrambles_in_dropback is true, rushes by a player who
                  threw a pass in the same game (the feed cannot separate scrambles from
                  designed QB runs, so the default is false)
  rush            `rush` and not dropback and not kneel

  epa_eligible    scrimmage, not kneel/spike/no_play, not garbage time
  pace_eligible   epa_eligible and not two_minute (when metrics.two_minute_filter)
"""

from __future__ import annotations

import polars as pl

from cfb_dfs.config import GarbageTime, MetricsConfig


def garbage_time_expr(
    gt: GarbageTime, period: str = "period", margin: str = "pos_score_diff_start"
) -> pl.Expr:
    m = pl.col(margin).abs()
    expr = pl.lit(False)
    for q, threshold in ((1, gt.q1), (2, gt.q2), (3, gt.q3), (4, gt.q4)):
        if threshold is not None:
            expr = expr | ((pl.col(period) == q) & (m > threshold))
    return expr.fill_null(False)


def prepare_plays(df: pl.DataFrame, metrics: MetricsConfig) -> pl.DataFrame:
    """Add boolean classification columns to a raw play-by-play frame."""
    df = df.with_columns(
        [
            (
                pl.col("clock.minutes").fill_null(0) * 60 + pl.col("clock.seconds").fill_null(0)
            ).alias("secs_left"),
            pl.col("scrimmage_play").fill_null(False).alias("is_scrimmage"),
            (
                pl.col("kneel_down").fill_null(False)
                | pl.col("text").fill_null("").str.to_lowercase().str.contains("kneel")
            ).alias("is_kneel"),
            pl.col("text").fill_null("").str.to_lowercase().str.contains("spike").alias("is_spike"),
            pl.col("penalty_no_play").fill_null(False).alias("is_no_play"),
            pl.col("pass").fill_null(False).alias("is_pass"),
            pl.col("rush").fill_null(False).alias("is_rush_raw"),
            pl.col("sack").fill_null(False).alias("is_sack"),
            garbage_time_expr(metrics.garbage_time).alias("is_garbage"),
        ]
    )
    df = df.with_columns(
        (pl.col("period").is_in([2, 4]) & (pl.col("secs_left") <= 120)).alias("is_two_minute")
    )

    if metrics.include_scrambles_in_dropback and "passer_player_name" in df.columns:
        passers = (
            df.filter(pl.col("is_pass") & pl.col("passer_player_name").is_not_null())
            .select(
                ["game_id", "pos_team_id", pl.col("passer_player_name").alias("rusher_player_name")]
            )
            .unique()
            .with_columns(pl.lit(True).alias("_qb"))
        )
        df = df.join(passers, on=["game_id", "pos_team_id", "rusher_player_name"], how="left")
        qb_rush = pl.col("is_rush_raw") & pl.col("_qb").fill_null(False) & ~pl.col("is_kneel")
        df = df.with_columns((pl.col("is_pass") | qb_rush).alias("is_dropback")).drop("_qb")
    else:
        df = df.with_columns(pl.col("is_pass").alias("is_dropback"))

    df = df.with_columns(
        (pl.col("is_rush_raw") & ~pl.col("is_dropback") & ~pl.col("is_kneel")).alias("is_rush")
    )
    clean = (
        pl.col("is_scrimmage")
        & ~pl.col("is_kneel")
        & ~pl.col("is_spike")
        & ~pl.col("is_no_play")
        & ~pl.col("is_garbage")
    )
    pace = clean & ~pl.col("is_two_minute") if metrics.two_minute_filter else clean
    return df.with_columns([clean.alias("epa_eligible"), pace.alias("pace_eligible")])


def describe_filters(metrics: MetricsConfig) -> str:
    gt = metrics.garbage_time
    parts = [
        f"garbage time = margin > Q1:{gt.q1 or 'never'} Q2:{gt.q2 or 'never'} "
        f"Q3:{gt.q3 or 'never'} Q4:{gt.q4 or 'never'}",
        "kneels, spikes, no-play penalties excluded",
        "final 2:00 of each half excluded from pace"
        if metrics.two_minute_filter
        else "no 2-minute filter",
        "QB rushes counted as dropbacks"
        if metrics.include_scrambles_in_dropback
        else "QB rushes counted as rushes (no scramble flag in feed)",
    ]
    return "; ".join(parts)
