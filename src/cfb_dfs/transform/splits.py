"""Season-to-date and last-N-game windows over per-team-game aggregates."""

from __future__ import annotations

import polars as pl


def add_game_order(team_games: pl.DataFrame, game_order: dict[int, int]) -> pl.DataFrame:
    """Attach a sortable game order (kickoff order); unknown games sort by id."""
    order = pl.DataFrame(
        {"game_id": list(game_order.keys()), "game_order": list(game_order.values())},
        schema={"game_id": pl.Int64, "game_order": pl.Int64},
    )
    df = team_games.with_columns(pl.col("game_id").cast(pl.Int64)).join(
        order, on="game_id", how="left"
    )
    return df.with_columns(pl.col("game_order").fill_null(pl.col("game_id")))


def window_sums(
    team_games: pl.DataFrame, sum_cols: list[str], window: int, key: str = "team_id"
) -> pl.DataFrame:
    """Return one row per team with `<col>` (season sum), `<col>_l{window}` (last-N sum)
    and `games`, `games_l{window}`."""
    df = team_games.sort([key, "game_order"])
    df = df.with_columns(
        (pl.col("game_order").rank("ordinal", descending=True).over(key)).alias("_recency")
    )
    recent = pl.col("_recency") <= window
    aggs: list[pl.Expr] = [pl.len().alias("games"), recent.sum().alias(f"games_l{window}")]
    for c in sum_cols:
        aggs.append(pl.col(c).sum().alias(c))
        aggs.append(pl.when(recent).then(pl.col(c)).otherwise(0).sum().alias(f"{c}_l{window}"))
    return df.group_by(key).agg(aggs).sort(key)


def ratio(num: str, den: str, alias: str, digits: int = 3) -> pl.Expr:
    return (
        pl.when(pl.col(den) > 0)
        .then(pl.col(num) / pl.col(den))
        .otherwise(None)
        .round(digits)
        .alias(alias)
    )
