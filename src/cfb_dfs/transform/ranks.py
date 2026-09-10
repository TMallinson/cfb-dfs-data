"""Ranks over a fixed population (FBS teams). Ties share the best (min) rank."""

from __future__ import annotations

import polars as pl


def add_rank(
    df: pl.DataFrame,
    col: str,
    alias: str,
    descending: bool,
    population: set[int],
    key: str = "team_id",
) -> pl.DataFrame:
    """Rank `col` among rows whose key is in `population`; others get null.
    descending=True means the highest value is rank 1."""
    in_pop = pl.col(key).is_in(list(population)) & pl.col(col).is_not_null()
    ranked = df.filter(in_pop).select(
        [key, pl.col(col).rank("min", descending=descending).cast(pl.Int64).alias(alias)]
    )
    return df.join(ranked, on=key, how="left")


def population_size(df: pl.DataFrame, col: str, population: set[int], key: str = "team_id") -> int:
    return df.filter(pl.col(key).is_in(list(population)) & pl.col(col).is_not_null()).height
