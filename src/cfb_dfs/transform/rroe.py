"""RROE% — rush rate over expected.

Model: gradient-boosted classifier predicting P(rush) for a scrimmage play from
situation: down, distance, yards to goal, score differential (offense
perspective), seconds left in the half, half, period, offense timeouts, plus the
pregame spread from the offense's perspective and the total when a line exists.
Training population: every dropback/rush play of the configured seasons with a
known FBS offense, excluding garbage time, kneels, spikes and no-play penalties
(the same `epa_eligible` flag the EPA metrics use). Holdout: 20% of games,
grouped by game.

Team RROE% = mean(actual_rush - P(rush)) x 100 over the current season's
early-down (1st and 2nd) eligible plays. Positive = runs more than the situation
predicts. Rank 1 = highest RROE% (most run-heavy over expectation), FBS only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import polars as pl
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import brier_score_loss, log_loss

from cfb_dfs.transform.ranks import add_rank
from cfb_dfs.transform.splits import add_game_order, window_sums

FEATURES = [
    "down",
    "distance",
    "yards_to_goal",
    "score_diff",
    "secs_left_half",
    "half",
    "period",
    "timeouts",
    "spread_off",
    "total",
    "has_line",
]


@dataclass
class ModelReport:
    n_train: int = 0
    n_holdout: int = 0
    n_games_train: int = 0
    n_games_holdout: int = 0
    base_rate: float = 0.0
    log_loss_model: float = 0.0
    log_loss_baseline: float = 0.0
    brier_model: float = 0.0
    brier_baseline: float = 0.0
    calibration: list[tuple[str, int, float, float]] = field(default_factory=list)
    seasons: list[int] = field(default_factory=list)
    trained_at: str = ""


def feature_frame(plays: pl.DataFrame, lines: pl.DataFrame | None = None) -> pl.DataFrame:
    """Model rows: eligible dropback/rush plays with situation + line features."""
    df = plays.filter(pl.col("epa_eligible") & (pl.col("is_dropback") | pl.col("is_rush")))
    secs_half = (
        pl.when(pl.col("period") == 1)
        .then(pl.col("secs_left") + 900)
        .when(pl.col("period") == 3)
        .then(pl.col("secs_left") + 900)
        .when(pl.col("period").is_in([2, 4]))
        .then(pl.col("secs_left"))
        .otherwise(0)
    )
    df = df.with_columns(
        [
            pl.col("start.yardsToEndzone").alias("yards_to_goal"),
            pl.col("pos_score_diff_start").alias("score_diff"),
            secs_half.alias("secs_left_half"),
            pl.when(pl.col("period") <= 2)
            .then(1)
            .when(pl.col("period") <= 4)
            .then(2)
            .otherwise(3)
            .alias("half"),
            pl.col("start.posTeamTimeouts").fill_null(3).alias("timeouts"),
            pl.col("is_rush").cast(pl.Int8).alias("y"),
        ]
    )
    if lines is not None and lines.height:
        df = df.join(lines, on="game_id", how="left")
        df = df.with_columns(
            [
                pl.when(pl.col("home_id") == pl.col("pos_team_id"))
                .then(pl.col("home_spread"))
                .when(pl.col("away_id") == pl.col("pos_team_id"))
                .then(-pl.col("home_spread"))
                .otherwise(None)
                .alias("spread_off"),
                pl.col("total").is_not_null().cast(pl.Int8).alias("has_line"),
            ]
        ).drop(["home_id", "away_id", "home_spread"])
    else:
        df = df.with_columns(
            [
                pl.lit(None, dtype=pl.Float64).alias("spread_off"),
                pl.lit(None, dtype=pl.Float64).alias("total"),
                pl.lit(0, dtype=pl.Int8).alias("has_line"),
            ]
        )
    return df.filter(pl.col("down").is_between(1, 4) & pl.col("yards_to_goal").is_not_null())


def lines_frame(rows: list[dict], preference: list[str]) -> pl.DataFrame:
    """game_id, home_id, away_id, home_spread (home perspective), total from CFBD /lines."""
    recs = []
    for g in rows:
        book = None
        by = {
            ln.get("provider", "").lower(): ln
            for ln in g.get("lines", [])
            if ln.get("spread") is not None
        }
        for p in preference:
            if p.lower() in by:
                book = by[p.lower()]
                break
        if book is None and by:
            book = next(iter(by.values()))
        recs.append(
            {
                "game_id": int(g["id"]),
                "home_id": int(g["homeTeamId"]),
                "away_id": int(g["awayTeamId"]),
                "home_spread": float(book["spread"]) if book else None,
                "total": float(book["overUnder"])
                if book and book.get("overUnder") is not None
                else None,
            }
        )
    schema = {
        "game_id": pl.Int64,
        "home_id": pl.Int64,
        "away_id": pl.Int64,
        "home_spread": pl.Float64,
        "total": pl.Float64,
    }
    return pl.DataFrame(recs, schema=schema, orient="row") if recs else pl.DataFrame(schema=schema)


def _xy(df: pl.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    # Missing line features become 0 with has_line = 0 so the model can tell them apart.
    x = (
        df.select(FEATURES)
        .with_columns([pl.col("spread_off").fill_null(0.0), pl.col("total").fill_null(0.0)])
        .to_numpy()
        .astype(float)
    )
    y = df["y"].to_numpy().astype(int)
    return x, y


def train(
    rows: pl.DataFrame, holdout_frac: float = 0.2, seed: int = 7
) -> tuple[HistGradientBoostingClassifier, ModelReport]:
    games = rows["game_id"].unique().sort().to_numpy()
    rng = np.random.default_rng(seed)
    hold = set(
        rng.choice(games, size=max(1, int(len(games) * holdout_frac)), replace=False).tolist()
    )
    tr = rows.filter(~pl.col("game_id").is_in(list(hold)))
    ho = rows.filter(pl.col("game_id").is_in(list(hold)))
    x_tr, y_tr = _xy(tr)
    x_ho, y_ho = _xy(ho)
    model = HistGradientBoostingClassifier(
        learning_rate=0.05,
        max_iter=300,
        max_leaf_nodes=31,
        min_samples_leaf=max(20, min(200, len(y_tr) // 50)),
        l2_regularization=1.0,
        early_stopping=False,
        random_state=seed,
    )
    model.fit(x_tr, y_tr)
    p = model.predict_proba(x_ho)[:, 1]
    base = float(y_tr.mean())
    rep = ModelReport(
        n_train=len(y_tr),
        n_holdout=len(y_ho),
        n_games_train=len(games) - len(hold),
        n_games_holdout=len(hold),
        base_rate=round(base, 4),
        log_loss_model=round(float(log_loss(y_ho, p, labels=[0, 1])), 4),
        log_loss_baseline=round(float(log_loss(y_ho, np.full_like(p, base), labels=[0, 1])), 4),
        brier_model=round(float(brier_score_loss(y_ho, p)), 4),
        brier_baseline=round(float(brier_score_loss(y_ho, np.full_like(p, base))), 4),
    )
    bins = np.linspace(0, 1, 11)
    which = np.clip(np.digitize(p, bins) - 1, 0, 9)
    for b in range(10):
        m = which == b
        if m.sum():
            rep.calibration.append(
                (
                    f"{bins[b]:.1f}-{bins[b + 1]:.1f}",
                    int(m.sum()),
                    round(float(p[m].mean()), 3),
                    round(float(y_ho[m].mean()), 3),
                )
            )
    # Refit on everything for the production predictions.
    x_all, y_all = _xy(rows)
    model.fit(x_all, y_all)
    return model, rep


def team_rroe(
    model: HistGradientBoostingClassifier,
    rows: pl.DataFrame,
    game_order: dict[int, int],
    fbs_ids: set[int],
    window: int = 3,
    early_downs: tuple[int, ...] = (1, 2),
) -> pl.DataFrame:
    df = rows.filter(pl.col("down").is_in(list(early_downs)))
    if df.height == 0:
        return pl.DataFrame({"team_id": []}, schema={"team_id": pl.Int64})
    x, _ = _xy(df)
    df = df.with_columns(pl.Series("p_rush", model.predict_proba(x)[:, 1]))
    tg = df.group_by(["game_id", pl.col("pos_team_id").alias("team_id")]).agg(
        [
            pl.len().alias("plays"),
            pl.col("y").sum().alias("rushes"),
            pl.col("p_rush").sum().alias("exp_rushes"),
        ]
    )
    tg = add_game_order(tg, game_order)
    w = window_sums(tg, ["plays", "rushes", "exp_rushes"], window)
    lw = f"_l{window}"
    out = w.with_columns(
        [
            (pl.col("rushes") / pl.col("plays")).round(3).alias("rush_rate"),
            (pl.col("exp_rushes") / pl.col("plays")).round(3).alias("exp_rush_rate"),
            ((pl.col("rushes") - pl.col("exp_rushes")) / pl.col("plays") * 100)
            .round(2)
            .alias("rroe"),
            pl.when(pl.col(f"plays{lw}") > 0)
            .then((pl.col(f"rushes{lw}") - pl.col(f"exp_rushes{lw}")) / pl.col(f"plays{lw}") * 100)
            .otherwise(None)
            .round(2)
            .alias(f"rroe{lw}"),
        ]
    )
    out = add_rank(out, "rroe", "rroe_rank", descending=True, population=fbs_ids)
    out = add_rank(out, f"rroe{lw}", f"rroe_rank{lw}", descending=True, population=fbs_ids)
    return out.sort("rroe_rank", nulls_last=True)


def report_markdown(rep: ModelReport) -> str:
    lines = [
        "# RROE model quality report",
        "",
        f"Trained {rep.trained_at} on seasons {rep.seasons}.",
        "",
        "| | value |",
        "|---|---|",
        f"| training plays / games | {rep.n_train:,} / {rep.n_games_train:,} |",
        f"| holdout plays / games (grouped by game) | {rep.n_holdout:,} / "
        f"{rep.n_games_holdout:,} |",
        f"| base rush rate | {rep.base_rate} |",
        f"| holdout log loss (model / constant baseline) | {rep.log_loss_model} / "
        f"{rep.log_loss_baseline} |",
        f"| holdout Brier (model / baseline) | {rep.brier_model} / {rep.brier_baseline} |",
        "",
        "Features: " + ", ".join(FEATURES),
        "",
        "## Calibration (holdout, 10 bins of predicted P(rush))",
        "",
        "| bin | plays | mean predicted | actual rush rate |",
        "|---|---|---|---|",
    ]
    for label, n, pred, actual in rep.calibration:
        lines.append(f"| {label} | {n:,} | {pred} | {actual} |")
    lines += [
        "",
        "Population: eligible dropback/rush plays (no garbage time, kneels, spikes, no-play",
        "penalties) with an FBS offense. Team RROE% uses 1st and 2nd downs only.",
    ]
    return "\n".join(lines) + "\n"
