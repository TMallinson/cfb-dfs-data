"""Player volume tables from CFBD /passing/plays, /rushing/plays and /roster.

Targets:  every pass attempt with a `targetId` (throwaways/spikes/unparsed
          attempts carry no target and are excluded from target counts but not
          from team attempts).
Passing:  per passer: attempts (spikes excluded), completions, yards, TD, INT,
          sacks taken (rushing feed rows with isSack and the passer as rusher),
          dropbacks = attempts + sacks, aDOT over attempts with air yards, PPA per
          attempt, plus designed runs from the rushing feed.
Rushing:  per rusher: individually attributed carries only (no sacks, kneels,
          team rushes, unmatched), yards, TD, success rate, PPA per carry, plus
          targets/receptions as a receiver.
Splits:   weekly columns Wk1..Wk16, season totals, per-game rates over games
          with at least one touch, last-3 team games.
"""

from __future__ import annotations

from typing import Any

import polars as pl

from cfb_dfs.transform.splits import add_game_order, window_sums
from cfb_dfs.transform.teams import TeamIndex

MAX_WEEK = 16
SKILL_POSITIONS = {"QB", "RB", "WR", "TE", "FB", "ATH", "HB", "SB"}


def roster_index(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        pid = str(r.get("id") or "")
        if not pid:
            continue
        out[pid] = {
            "name": f"{r.get('firstName') or ''} {r.get('lastName') or ''}".strip(),
            "position": r.get("position"),
            "team": r.get("team"),
            "year": r.get("year"),
        }
    return out


def _team_id(teams: TeamIndex, school: Any) -> int | None:
    t = teams.by_school.get(str(school or ""))
    return t.id if t else None


def passing_frame(rows: list[dict[str, Any]], teams: TeamIndex) -> pl.DataFrame:
    recs = []
    for p in rows:
        recs.append(
            {
                "game_id": int(p["gameId"]),
                "week": p.get("week"),
                "team_id": _team_id(teams, p.get("offense")),
                "passer_id": str(p.get("passerId") or "") or None,
                "passer": p.get("passer"),
                "target_id": str(p.get("targetId") or "") or None,
                "target": p.get("target"),
                "outcome": p.get("outcome"),
                "air_yards": p.get("airYards"),
                "yards": p.get("totalYards"),
                "yac": p.get("yardsAfterCatch"),
                "ppa": p.get("ppa"),
                "is_spike": bool(p.get("isSpike")),
                "is_throwaway": bool(p.get("isThrowaway")),
                "target_ytg": p.get("targetYardsToGoal"),
                "text": p.get("playText") or "",
            }
        )
    schema = {
        "game_id": pl.Int64,
        "week": pl.Int64,
        "team_id": pl.Int64,
        "passer_id": pl.Utf8,
        "passer": pl.Utf8,
        "target_id": pl.Utf8,
        "target": pl.Utf8,
        "outcome": pl.Utf8,
        "air_yards": pl.Float64,
        "yards": pl.Float64,
        "yac": pl.Float64,
        "ppa": pl.Float64,
        "is_spike": pl.Boolean,
        "is_throwaway": pl.Boolean,
        "target_ytg": pl.Float64,
        "text": pl.Utf8,
    }
    df = pl.DataFrame(recs, schema=schema, orient="row") if recs else pl.DataFrame(schema=schema)
    return df.filter(pl.col("team_id").is_not_null() & ~pl.col("is_spike")).with_columns(
        [
            (pl.col("outcome") == "completion").alias("is_completion"),
            (pl.col("outcome") == "interception").alias("is_int"),
            pl.col("text").str.to_lowercase().str.contains("touchdown").alias("is_td"),
        ]
    )


def rushing_frame(rows: list[dict[str, Any]], teams: TeamIndex) -> pl.DataFrame:
    recs = []
    for r in rows:
        recs.append(
            {
                "game_id": int(r["gameId"]),
                "week": r.get("week"),
                "team_id": _team_id(teams, r.get("offense")),
                "rusher_id": str(r.get("rusherId") or "") or None,
                "rusher": r.get("rusher"),
                "yards": r.get("rusherYards", r.get("rushingYards")),
                "is_td": bool(r.get("isRushingTouchdown")),
                "is_sack": bool(r.get("isSack")),
                "is_kneel": bool(r.get("isKneel")),
                "is_team": bool(r.get("isTeamRush")),
                "attribution": r.get("attributionStatus"),
                "ppa": r.get("ppa"),
                "success": r.get("success"),
            }
        )
    schema = {
        "game_id": pl.Int64,
        "week": pl.Int64,
        "team_id": pl.Int64,
        "rusher_id": pl.Utf8,
        "rusher": pl.Utf8,
        "yards": pl.Float64,
        "is_td": pl.Boolean,
        "is_sack": pl.Boolean,
        "is_kneel": pl.Boolean,
        "is_team": pl.Boolean,
        "attribution": pl.Utf8,
        "ppa": pl.Float64,
        "success": pl.Boolean,
    }
    df = pl.DataFrame(recs, schema=schema, orient="row") if recs else pl.DataFrame(schema=schema)
    return df.filter(pl.col("team_id").is_not_null())


# -- per-player-game aggregates ---------------------------------------------------------------


def target_games(passing: pl.DataFrame, rushing: pl.DataFrame | None = None) -> pl.DataFrame:
    """Per receiver-game. `team_db` = team dropbacks (attempts + sacks) in that game, the
    denominator for the targets-per-team-dropback proxy (routes are not available free)."""
    df = passing.filter(pl.col("target_id").is_not_null())
    team_att = passing.group_by(["game_id", "team_id"]).agg(pl.len().alias("team_attempts"))
    if rushing is not None and rushing.height:
        sacks = (
            rushing.filter(pl.col("is_sack"))
            .group_by(["game_id", "team_id"])
            .agg(pl.len().alias("team_sacks"))
        )
        team_att = (
            team_att.join(sacks, on=["game_id", "team_id"], how="left")
            .with_columns(
                (pl.col("team_attempts") + pl.col("team_sacks").fill_null(0)).alias("team_db")
            )
            .drop("team_sacks")
        )
    else:
        team_att = team_att.with_columns(pl.col("team_attempts").alias("team_db"))
    return (
        df.group_by(["game_id", "week", "team_id", pl.col("target_id").alias("player_id")])
        .agg(
            [
                pl.col("target").first().alias("name"),
                pl.len().alias("targets"),
                pl.col("is_completion").sum().alias("receptions"),
                pl.when(pl.col("is_completion"))
                .then(pl.col("yards"))
                .otherwise(0)
                .sum()
                .alias("rec_yards"),
                (pl.col("is_completion") & pl.col("is_td")).sum().alias("rec_td"),
                pl.col("air_yards").sum().alias("air_yards"),
                pl.col("air_yards").is_not_null().sum().alias("air_yards_n"),
                pl.when(pl.col("is_completion"))
                .then(pl.col("yac"))
                .otherwise(0)
                .sum()
                .alias("yac"),
                pl.col("ppa").sum().alias("ppa"),
                (pl.col("target_ytg") <= 20).sum().alias("rz_targets"),
            ]
        )
        .join(team_att, on=["game_id", "team_id"], how="left")
    )


def passer_games(passing: pl.DataFrame, rushing: pl.DataFrame) -> pl.DataFrame:
    df = passing.filter(pl.col("passer_id").is_not_null())
    base = df.group_by(["game_id", "week", "team_id", pl.col("passer_id").alias("player_id")]).agg(
        [
            pl.col("passer").first().alias("name"),
            pl.len().alias("attempts"),
            pl.col("is_completion").sum().alias("completions"),
            pl.when(pl.col("is_completion"))
            .then(pl.col("yards"))
            .otherwise(0)
            .sum()
            .alias("pass_yards"),
            (pl.col("is_completion") & pl.col("is_td")).sum().alias("pass_td"),
            pl.col("is_int").sum().alias("ints"),
            pl.col("air_yards").sum().alias("air_yards"),
            pl.col("air_yards").is_not_null().sum().alias("air_yards_n"),
            pl.col("ppa").sum().alias("ppa"),
            pl.col("is_throwaway").sum().alias("throwaways"),
        ]
    )
    sacks = (
        rushing.filter(pl.col("is_sack") & pl.col("rusher_id").is_not_null())
        .group_by(["game_id", pl.col("rusher_id").alias("player_id")])
        .agg(pl.len().alias("sacks"))
    )
    runs = (
        rushing.filter(
            ~pl.col("is_sack") & ~pl.col("is_kneel") & (pl.col("attribution") == "individual")
        )
        .group_by(["game_id", pl.col("rusher_id").alias("player_id")])
        .agg([pl.len().alias("rush_att"), pl.col("yards").sum().alias("rush_yards")])
    )
    out = base.join(sacks, on=["game_id", "player_id"], how="left").join(
        runs, on=["game_id", "player_id"], how="left"
    )
    return out.with_columns(
        [
            pl.col("sacks").fill_null(0),
            pl.col("rush_att").fill_null(0),
            pl.col("rush_yards").fill_null(0),
        ]
    ).with_columns((pl.col("attempts") + pl.col("sacks")).alias("dropbacks"))


def rusher_games(rushing: pl.DataFrame, passing: pl.DataFrame) -> pl.DataFrame:
    df = rushing.filter(
        ~pl.col("is_sack")
        & ~pl.col("is_kneel")
        & ~pl.col("is_team")
        & (pl.col("attribution") == "individual")
        & pl.col("rusher_id").is_not_null()
    )
    base = df.group_by(["game_id", "week", "team_id", pl.col("rusher_id").alias("player_id")]).agg(
        [
            pl.col("rusher").first().alias("name"),
            pl.len().alias("carries"),
            pl.col("yards").sum().alias("rush_yards"),
            pl.col("is_td").sum().alias("rush_td"),
            pl.col("success").fill_null(False).sum().alias("successes"),
            pl.col("ppa").sum().alias("ppa"),
        ]
    )
    rec = (
        passing.filter(pl.col("target_id").is_not_null())
        .group_by(["game_id", pl.col("target_id").alias("player_id")])
        .agg(
            [
                pl.len().alias("targets"),
                pl.col("is_completion").sum().alias("receptions"),
                pl.when(pl.col("is_completion"))
                .then(pl.col("yards"))
                .otherwise(0)
                .sum()
                .alias("rec_yards"),
            ]
        )
    )
    out = base.join(rec, on=["game_id", "player_id"], how="left")
    return out.with_columns(
        [
            pl.col("targets").fill_null(0),
            pl.col("receptions").fill_null(0),
            pl.col("rec_yards").fill_null(0),
        ]
    )


# -- season tables ------------------------------------------------------------------------------


def weekly_pivot(pg: pl.DataFrame, value: str) -> pl.DataFrame:
    """player_id -> Wk1..Wk16 columns of `value`."""
    wide = pg.select(["player_id", "week", value]).pivot(
        on="week", index="player_id", values=value, aggregate_function="sum"
    )
    for wk in range(1, MAX_WEEK + 1):
        if str(wk) not in wide.columns:
            wide = wide.with_columns(pl.lit(None, dtype=pl.Int64).alias(str(wk)))
    return wide.select(
        ["player_id", *[pl.col(str(w)).alias(f"wk{w}") for w in range(1, MAX_WEEK + 1)]]
    )


def season_table(
    pg: pl.DataFrame,
    volume: str,
    sum_cols: list[str],
    game_order: dict[int, int],
    team_games: dict[int, int],
    window: int = 3,
) -> pl.DataFrame:
    """One row per player: identity, games, weekly volume, season sums, last-N sums."""
    if pg.height == 0:
        return pl.DataFrame({"player_id": []}, schema={"player_id": pl.Utf8})
    ordered = add_game_order(pg, game_order)
    sums = window_sums(ordered, sum_cols, window, key="player_id")
    ident = pg.group_by("player_id").agg(
        [
            pl.col("name").first().alias("name"),
            pl.col("team_id").mode().first().alias("team_id"),
        ]
    )
    tg = pl.DataFrame(
        {"team_id": list(team_games.keys()), "team_games": list(team_games.values())},
        schema={"team_id": pl.Int64, "team_games": pl.Int64},
    )
    out = ident.join(sums, on="player_id").join(
        weekly_pivot(pg, volume), on="player_id", how="left"
    )
    out = out.join(tg, on="team_id", how="left")
    return out.sort(["team_id", volume], descending=[False, True])


def team_game_counts(pg_frames: list[pl.DataFrame]) -> dict[int, int]:
    pairs = pl.concat([f.select(["game_id", "team_id"]) for f in pg_frames if f.height]).unique()
    counts = pairs.group_by("team_id").agg(pl.len().alias("n"))
    return {int(r["team_id"]): int(r["n"]) for r in counts.to_dicts()}


def per_game(num: str, den: str, digits: int = 2) -> pl.Expr:
    return pl.when(pl.col(den) > 0).then(pl.col(num) / pl.col(den)).otherwise(None).round(digits)
