"""Normalize CFBD /plays and /drives into the frames the transforms consume.

Why CFBD and not the sportsdataverse ESPN parquet for team metrics: on
2026-09-09 the ESPN-derived feed was missing most plays for 33 of 97 week-1
games, while CFBD had every play of all 203 games. Both ultimately come from
ESPN, and in both the *play* clock is frequently stale (identical timestamps
for long runs of plays), so pace is computed from drive elapsed time instead of
play-to-play clock deltas (see pace.py).

Normalized play columns (shared with filters.py):
  game_id, week, game_play_number, period, clock.minutes, clock.seconds,
  pos_team_id, def_pos_team_id, pos_team, def_pos_team, drive_id,
  pos_score_diff_start, down, distance, start.yardsToEndzone,
  start.posTeamTimeouts, scrimmage_play, pass, rush, sack, kneel_down,
  penalty_no_play, type.text, text, EPA (= CFBD ppa), wallclock
"""

from __future__ import annotations

from typing import Any

import polars as pl

from cfb_dfs.logging_setup import get_logger
from cfb_dfs.transform.teams import TeamIndex

log = get_logger(__name__)

PASS_TYPES = {
    "Pass Reception",
    "Pass Incompletion",
    "Passing Touchdown",
    "Sack",
    "Pass Completion",
    "Pass Interception Return",
    "Pass Interception",
    "Interception",
    "Interception Return Touchdown",
}
RUSH_TYPES = {"Rush", "Rushing Touchdown"}
# Scrimmage plays whose pass/rush nature CFBD's playType loses (kept for play counts,
# excluded from the EPA buckets).
OTHER_SCRIMMAGE = {
    "Fumble Recovery (Own)",
    "Fumble Recovery (Opponent)",
    "Fumble",
    "Fumble Return Touchdown",
    "Safety",
}
SCRIMMAGE_TYPES = PASS_TYPES | RUSH_TYPES | OTHER_SCRIMMAGE
SACK_TYPES = {"Sack"}


def _tid(teams: TeamIndex, school: str | None) -> int | None:
    t = teams.by_school.get(school or "")
    return t.id if t else None


def normalize_plays(
    rows: list[dict[str, Any]], teams: TeamIndex, week: int | None = None
) -> pl.DataFrame:
    recs: list[dict[str, Any]] = []
    unknown: set[str] = set()
    for p in rows:
        off_id, def_id = _tid(teams, p.get("offense")), _tid(teams, p.get("defense"))
        if off_id is None:
            unknown.add(str(p.get("offense")))
        if def_id is None:
            unknown.add(str(p.get("defense")))
        clock = p.get("clock") or {}
        ptype = p.get("playType") or ""
        text = p.get("playText") or ""
        low = text.lower()
        recs.append(
            {
                "game_id": int(p["gameId"]),
                "week": week,
                "drive_id": str(p.get("driveId")),
                "drive_number": p.get("driveNumber"),
                "play_number": p.get("playNumber"),
                "period": p.get("period"),
                "clock.minutes": clock.get("minutes"),
                "clock.seconds": clock.get("seconds"),
                "pos_team_id": off_id,
                "def_pos_team_id": def_id,
                "pos_team": p.get("offense"),
                "def_pos_team": p.get("defense"),
                "pos_score_diff_start": (p.get("offenseScore") or 0) - (p.get("defenseScore") or 0),
                "down": p.get("down"),
                "distance": p.get("distance"),
                "start.yardsToEndzone": p.get("yardsToGoal"),
                "start.posTeamTimeouts": p.get("offenseTimeouts"),
                "scrimmage_play": ptype in SCRIMMAGE_TYPES,
                "pass": ptype in PASS_TYPES,
                "rush": ptype in RUSH_TYPES,
                "sack": ptype in SACK_TYPES,
                "kneel_down": "kneel" in low,
                "penalty_no_play": ptype == "Penalty",
                "type.text": ptype,
                "text": text,
                "EPA": p.get("ppa"),
                "yards_gained": p.get("yardsGained"),
                "wallclock": p.get("wallclock"),
            }
        )
    if unknown:  # non-D1 opponents (NAIA etc.) are not in CFBD /teams; their plays are dropped
        log.info("plays.unknown_team_names", names=sorted(unknown)[:10], count=len(unknown))
    schema = {
        "game_id": pl.Int64,
        "week": pl.Int64,
        "drive_id": pl.Utf8,
        "drive_number": pl.Int64,
        "play_number": pl.Int64,
        "period": pl.Int64,
        "clock.minutes": pl.Int64,
        "clock.seconds": pl.Int64,
        "pos_team_id": pl.Int64,
        "def_pos_team_id": pl.Int64,
        "pos_team": pl.Utf8,
        "def_pos_team": pl.Utf8,
        "pos_score_diff_start": pl.Int64,
        "down": pl.Int64,
        "distance": pl.Int64,
        "start.yardsToEndzone": pl.Int64,
        "start.posTeamTimeouts": pl.Int64,
        "scrimmage_play": pl.Boolean,
        "pass": pl.Boolean,
        "rush": pl.Boolean,
        "sack": pl.Boolean,
        "kneel_down": pl.Boolean,
        "penalty_no_play": pl.Boolean,
        "type.text": pl.Utf8,
        "text": pl.Utf8,
        "EPA": pl.Float64,
        "yards_gained": pl.Int64,
        "wallclock": pl.Utf8,
    }
    df = pl.DataFrame(recs, schema=schema, orient="row") if recs else pl.DataFrame(schema=schema)
    df = (
        df.filter(pl.col("pos_team_id").is_not_null() & pl.col("def_pos_team_id").is_not_null())
        .sort(["game_id", "drive_number", "play_number"])
        .with_columns(pl.int_range(pl.len()).over("game_id").alias("game_play_number"))
    )
    return df


def normalize_drives(rows: list[dict[str, Any]], teams: TeamIndex) -> pl.DataFrame:
    recs: list[dict[str, Any]] = []
    for d in rows:
        el, st = d.get("elapsed") or {}, d.get("startTime") or {}
        recs.append(
            {
                "game_id": int(d["gameId"]),
                "drive_id": str(d.get("id")),
                "team_id": _tid(teams, d.get("offense")),
                "start_period": d.get("startPeriod"),
                "start_secs": (st.get("minutes") or 0) * 60 + (st.get("seconds") or 0),
                "elapsed_secs": (el.get("minutes") or 0) * 60 + (el.get("seconds") or 0),
                "drive_plays": d.get("plays"),
                "start_margin": (d.get("startOffenseScore") or 0)
                - (d.get("startDefenseScore") or 0),
                "result": d.get("driveResult"),
            }
        )
    schema = {
        "game_id": pl.Int64,
        "drive_id": pl.Utf8,
        "team_id": pl.Int64,
        "start_period": pl.Int64,
        "start_secs": pl.Int64,
        "elapsed_secs": pl.Int64,
        "drive_plays": pl.Int64,
        "start_margin": pl.Int64,
        "result": pl.Utf8,
    }
    return pl.DataFrame(recs, schema=schema, orient="row") if recs else pl.DataFrame(schema=schema)
