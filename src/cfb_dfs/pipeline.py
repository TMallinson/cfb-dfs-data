"""Pipeline orchestration.

Contract: each stage fetches + transforms and records its outcome; a stage
failure is logged and the remaining stages still run; the sheet is written
once at the end (or diffed on --dry-run); exit code is non-zero if any stage
failed. Stages: slates, vegas (Phase 3); pace, epa (Phase 4); rroe (Phase 5);
players (Phase 6).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import polars as pl

from cfb_dfs.config import Secrets, Settings
from cfb_dfs.logging_setup import get_logger
from cfb_dfs.models import Game, GameLines, Team
from cfb_dfs.models.slates import Slate
from cfb_dfs.sheets.layout import TabSpec, build_specs
from cfb_dfs.sheets.writer import Cell, SheetWriter, TableWrite
from cfb_dfs.sources.cache import DiskCache
from cfb_dfs.sources.cfbd import CfbdClient
from cfb_dfs.sources.http import SourceError
from cfb_dfs.transform.filters import describe_filters, prepare_plays
from cfb_dfs.transform.slates import GameSlates, assign, manual_slates_from_rows
from cfb_dfs.transform.teams import Overrides, TeamIndex
from cfb_dfs.transform.vegas import TeamLine, all_team_lines

log = get_logger(__name__)

ALL_STAGES = ["slates", "vegas", "weather", "pace", "epa", "rroe", "players"]
IMPLEMENTED = {"slates", "vegas", "weather", "pace", "epa", "rroe", "players"}
OVERRIDES_PATH = "data/overrides/team_aliases.yaml"
MAIN_NCOLS = 34
RROE_REPORT_PATH = "reports/rroe_model_report.md"


@dataclass
class RunResult:
    week: int
    stages_ok: list[str] = field(default_factory=list)
    stages_failed: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    rows_written: dict[str, int] = field(default_factory=dict)
    sources_used: list[str] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def exit_code(self) -> int:
        return 1 if self.stages_failed else 0

    @property
    def status(self) -> str:
        if not self.stages_failed:
            return "OK"
        return "PARTIAL: " + ", ".join(f"{k} failed" for k in self.stages_failed)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        log.warning("pipeline.warning", msg=msg)


@dataclass
class Context:
    settings: Settings
    secrets: Secrets
    cache: DiskCache
    cfbd: CfbdClient | None
    week: int
    result: RunResult
    teams: TeamIndex | None = None
    games: list[Game] = field(default_factory=list)
    season_games: list[Game] = field(default_factory=list)
    slates: list[GameSlates] = field(default_factory=list)
    dk_slates: list[Slate] = field(default_factory=list)
    lines: dict[int, list[TeamLine]] = field(default_factory=dict)
    line_source: str = "none"
    slate_source: str = "none"
    config_rows: list[list[Any]] = field(default_factory=list)
    plays: pl.DataFrame | None = None
    drives: pl.DataFrame | None = None
    pace: pl.DataFrame | None = None
    epa: pl.DataFrame | None = None
    ppa: dict[int, dict[str, float | None]] = field(default_factory=dict)
    weather: dict[int, Any] = field(default_factory=dict)
    rroe: pl.DataFrame | None = None
    rroe_report: Any = None
    players: dict[str, pl.DataFrame] = field(default_factory=dict)
    roster: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def fbs_ids(self) -> set[int]:
        return self.teams.fbs_ids() if self.teams else set()

    def game_order(self) -> dict[int, int]:
        ordered = sorted(self.season_games, key=lambda g: (g.start_date, g.id))
        return {g.id: i for i, g in enumerate(ordered)}


# -- foundation ------------------------------------------------------------------------------


def load_teams(ctx: Context) -> TeamIndex:
    overrides = Overrides.load(OVERRIDES_PATH)
    teams: list[Team] = []
    if ctx.cfbd is not None:
        teams = ctx.cfbd.teams(ctx.settings.season)
        ctx.result.sources_used.append("cfbd:/teams")
    if not teams:
        raise SourceError("No team list available (CFBD /teams failed)")
    return TeamIndex(teams, overrides)


def load_games(ctx: Context) -> list[Game]:
    s = ctx.settings
    if ctx.cfbd is not None:
        try:
            games = ctx.cfbd.games(s.season, ctx.week, s.season_type, classification="fbs")
            if games:
                ctx.result.sources_used.append("cfbd:/games")
                return games
        except SourceError as exc:
            ctx.result.warn(f"CFBD /games failed: {exc}")
    if s.sources.espn.enabled:
        from cfb_dfs.sources.espn import fetch_scoreboard, scoreboard_to_games_and_lines

        payload = fetch_scoreboard(ctx.cache, s.cache.ttl_hours, s.season, ctx.week)
        games, _ = scoreboard_to_games_and_lines(payload)
        ctx.result.sources_used.append("espn:scoreboard(games)")
        return games
    raise SourceError("No schedule source available")


def load_season_games(ctx: Context) -> list[Game]:
    """Whole-season FBS schedule (one cached CFBD call) for game ordering / last-N."""
    if ctx.cfbd is None:
        return list(ctx.games)
    try:
        games = ctx.cfbd.games(ctx.settings.season, None, ctx.settings.season_type, "fbs")
    except SourceError as exc:
        ctx.result.warn(f"CFBD season schedule failed, last-N uses week order: {exc}")
        return list(ctx.games)
    return games


def load_plays(ctx: Context) -> pl.DataFrame:
    """Season play-by-play + drives from CFBD (one call per week each, cached), normalized
    and classified once, then shared by pace/epa/rroe."""
    if ctx.plays is not None:
        return ctx.plays
    from cfb_dfs.transform.plays import normalize_drives, normalize_plays

    s = ctx.settings
    if ctx.cfbd is None:
        raise SourceError("CFBD client disabled; play-by-play unavailable")
    assert ctx.teams is not None
    if not ctx.season_games:
        ctx.season_games = load_season_games(ctx)
    now = s.now()
    weeks = sorted({g.week for g in ctx.season_games if g.start_date < now} | {ctx.week})
    plays_frames: list[pl.DataFrame] = []
    drive_frames: list[pl.DataFrame] = []
    for wk in weeks:
        completed = wk < ctx.week
        rows = ctx.cfbd.plays(s.season, wk, s.season_type, completed=completed)
        if rows:
            plays_frames.append(normalize_plays(rows, ctx.teams, week=wk))
        try:
            drows = ctx.cfbd.drives(s.season, wk, s.season_type, completed=completed)
        except SourceError as exc:
            ctx.result.warn(f"CFBD /drives week {wk} failed (pace may be incomplete): {exc}")
            drows = []
        if drows:
            drive_frames.append(normalize_drives(drows, ctx.teams))
    if not plays_frames:
        raise SourceError("CFBD /plays returned no plays for any week")
    ctx.plays = prepare_plays(pl.concat(plays_frames, how="vertical"), s.metrics)
    ctx.drives = pl.concat(drive_frames, how="vertical") if drive_frames else None
    n_games = ctx.plays["game_id"].n_unique()
    ctx.result.sources_used.append(
        f"cfbd:/plays+/drives(weeks {weeks[0]}-{weeks[-1]}, {n_games} games)"
    )
    _report_pbp_coverage(ctx)
    return ctx.plays


def _report_pbp_coverage(ctx: Context) -> None:
    assert ctx.plays is not None
    have = set(ctx.plays["game_id"].unique().to_list())
    now = ctx.settings.now()
    completed = [g for g in ctx.season_games if g.completed or g.start_date < now]
    missing = [g for g in completed if g.id not in have]
    if missing:
        names = ", ".join(f"{g.away_team}@{g.home_team}" for g in missing[:8])
        ctx.result.warn(
            f"{len(missing)} completed FBS game(s) missing from play-by-play feed: {names}"
        )


# -- stages ----------------------------------------------------------------------------------


def stage_slates(ctx: Context) -> None:
    s = ctx.settings
    assert ctx.teams is not None
    dk: list[Slate] = []
    if s.sources.draftkings.enabled:
        from cfb_dfs.sources.draftkings import fetch_slates

        try:
            dk = fetch_slates(ctx.cache, s.cache.ttl_hours, s.season, ctx.week)
            ctx.result.sources_used.append(f"draftkings:{len(dk)} slates")
            pairs = []
            for sl in dk:
                for g in sl.games:
                    for side in (g.away, g.home):
                        t = ctx.teams.match_dk(side)
                        if t is not None:
                            pairs.append((t.id, side.abbreviation))
            ctx.teams.adopt_dk_abbreviations(pairs)
        except SourceError as exc:
            ctx.result.warn(f"DraftKings slates unavailable: {exc}")
    ctx.dk_slates = dk
    manual = manual_slates_from_rows(ctx.config_rows, ctx.games, ctx.teams, s.tz)
    if manual:
        ctx.result.sources_used.append(f"config-tab:{len(manual)} slates")
    assigned, warnings = assign(ctx.games, dk, manual, s.slates.kickoff_windows_ct, ctx.teams, s.tz)
    for w in warnings:
        ctx.result.warn(w)
    if not assigned:
        raise SourceError("No games assigned to any slate")
    ctx.slates = assigned
    ctx.slate_source = "draftkings" if dk else ("config" if manual else "kickoff-window")
    log.info(
        "stage.slates",
        games=len(assigned),
        source=ctx.slate_source,
        labels=sorted({gs.primary for gs in assigned if gs.primary}),
    )


def stage_vegas(ctx: Context) -> None:
    s = ctx.settings
    lines: list[GameLines] = []
    source = "none"
    if ctx.cfbd is not None:
        try:
            lines = ctx.cfbd.lines(s.season, ctx.week, s.season_type)
            source = "cfbd:/lines"
        except SourceError as exc:
            ctx.result.warn(f"CFBD /lines failed: {exc}")
    if not lines and s.sources.espn.enabled:
        from cfb_dfs.sources.espn import fetch_scoreboard, scoreboard_to_games_and_lines

        try:
            payload = fetch_scoreboard(ctx.cache, s.cache.ttl_hours, s.season, ctx.week)
            _, lines = scoreboard_to_games_and_lines(payload)
            source = "espn:scoreboard(odds)"
        except SourceError as exc:
            ctx.result.warn(f"ESPN scoreboard failed: {exc}")
    if not lines:
        raise SourceError("No betting lines from any source")
    ctx.result.sources_used.append(source)
    ctx.line_source = source
    ctx.lines = all_team_lines(lines, s.sources.cfbd.line_provider_preference)
    with_line = sum(1 for tl in ctx.lines.values() if tl[0].total is not None)
    log.info("stage.vegas", games=len(lines), with_total=with_line, source=source)
    if with_line == 0:
        raise SourceError("Lines source returned games but no totals")


def stage_pace(ctx: Context) -> None:
    from cfb_dfs.transform.pace import team_pace

    plays = load_plays(ctx)
    if ctx.drives is None or ctx.drives.height == 0:
        raise SourceError("No drive data; pace needs CFBD /drives")
    ctx.pace = team_pace(
        ctx.drives,
        plays,
        ctx.settings.metrics,
        ctx.game_order(),
        ctx.fbs_ids,
        ctx.settings.metrics.recent_games_window,
    )
    ranked = ctx.pace.filter(pl.col("pace_rank").is_not_null()).height
    log.info("stage.pace", teams=ctx.pace.height, ranked_fbs=ranked)
    if ranked == 0:
        raise SourceError("Pace computed for zero FBS teams")


def stage_epa(ctx: Context) -> None:
    from cfb_dfs.transform.epa import team_epa

    plays = load_plays(ctx)
    ctx.epa = team_epa(
        plays, ctx.game_order(), ctx.fbs_ids, ctx.settings.metrics.recent_games_window
    )
    ranked = ctx.epa.filter(pl.col("off_epa_play_rank").is_not_null()).height
    log.info("stage.epa", teams=ctx.epa.height, ranked_fbs=ranked)
    if ranked == 0:
        raise SourceError("EPA computed for zero FBS teams")
    if ctx.cfbd is not None and ctx.teams is not None:
        try:
            rows = ctx.cfbd.ppa_teams(ctx.settings.season, exclude_garbage_time=True)
            for r in rows:
                t = ctx.teams.by_school.get(r.get("team", ""))
                if t is None:
                    continue
                off, de = r.get("offense", {}) or {}, r.get("defense", {}) or {}
                ctx.ppa[t.id] = {
                    "off_pass": off.get("passing"),
                    "off_rush": off.get("rushing"),
                    "def_pass": de.get("passing"),
                    "def_rush": de.get("rushing"),
                }
            ctx.result.sources_used.append("cfbd:/ppa/teams")
        except SourceError as exc:
            ctx.result.warn(f"CFBD PPA cross-check unavailable: {exc}")


def stage_weather(ctx: Context) -> None:
    from cfb_dfs.sources.openmeteo import fetch_forecasts
    from cfb_dfs.transform.weather import summarize, venues_from_rows

    s = ctx.settings
    if not s.sources.openmeteo.enabled:
        raise SourceError("Open-Meteo disabled in config")
    if ctx.cfbd is None:
        raise SourceError("CFBD client disabled; venue coordinates unavailable")
    venues = venues_from_rows(ctx.cfbd.venues())
    games = [gs.game for gs in ctx.slates] or ctx.games
    points: list[tuple[float, float]] = []
    point_index: dict[int, int] = {}
    for g in games:
        v = venues.get(g.venue_id or -1)
        if v and not v.dome and v.latitude is not None and v.longitude is not None:
            key = (round(v.latitude, 3), round(v.longitude, 3))
            if key not in points:
                points.append(key)
            point_index[g.id] = points.index(key)
    forecasts = fetch_forecasts(ctx.cache, s.season, ctx.week, points)
    for g in games:
        fc = forecasts[point_index[g.id]] if g.id in point_index else None
        ctx.weather[g.id] = summarize(g, venues.get(g.venue_id or -1), fc)
    n_ok = sum(1 for w in ctx.weather.values() if w.temp_f is not None or w.dome)
    ctx.result.sources_used.append(f"open-meteo:{len(points)} venues")
    log.info("stage.weather", games=len(ctx.weather), with_forecast=n_ok)
    if games and n_ok == 0:
        raise SourceError("No game received a forecast")


def stage_rroe(ctx: Context) -> None:
    from cfb_dfs.transform.plays import normalize_plays
    from cfb_dfs.transform.rroe import (
        feature_frame,
        lines_frame,
        report_markdown,
        team_rroe,
        train,
    )

    s = ctx.settings
    cfg = s.metrics.rroe
    assert ctx.cfbd is not None and ctx.teams is not None
    current = load_plays(ctx)
    pref = s.sources.cfbd.line_provider_preference
    frames: list[pl.DataFrame] = []
    for season in cfg.train_seasons:
        if season == s.season:
            plays = current
            lines = lines_frame(ctx.cfbd.lines_season(season), pref)
        else:
            weeks = range(1, 17)
            parts = []
            for wk in weeks:
                raw = ctx.cfbd.plays(season, wk, s.season_type, completed=True, prior_season=True)
                if raw:
                    parts.append(normalize_plays(raw, ctx.teams, week=wk))
            if not parts:
                ctx.result.warn(f"no plays for training season {season}")
                continue
            plays = prepare_plays(pl.concat(parts, how="vertical"), s.metrics)
            lines = lines_frame(ctx.cfbd.lines_season(season, prior_season=True), pref)
        ff = feature_frame(plays, lines).filter(pl.col("pos_team_id").is_in(list(ctx.fbs_ids)))
        frames.append(ff.with_columns(pl.lit(season).alias("season")))
        log.info("rroe.training_data", season=season, plays=ff.height)
    rows = pl.concat(frames, how="vertical")
    model, rep = train(rows, holdout_frac=cfg.holdout_fraction)
    rep.seasons = list(cfg.train_seasons)
    rep.trained_at = f"{s.now():%Y-%m-%d %H:%M %Z}"
    ctx.rroe_report = rep
    cur_rows = rows.filter(pl.col("season") == s.season)
    ctx.rroe = team_rroe(
        model,
        cur_rows,
        ctx.game_order(),
        ctx.fbs_ids,
        s.metrics.recent_games_window,
        tuple(cfg.early_downs),
    )
    try:
        from pathlib import Path

        Path(RROE_REPORT_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(RROE_REPORT_PATH).write_text(report_markdown(rep), encoding="utf-8")
    except OSError as exc:
        ctx.result.warn(f"could not write RROE report: {exc}")
    ctx.result.sources_used.append(
        f"rroe-model(train {rep.n_train} plays, ll {rep.log_loss_model})"
    )
    log.info(
        "stage.rroe",
        teams=ctx.rroe.height,
        log_loss=rep.log_loss_model,
        baseline=rep.log_loss_baseline,
        brier=rep.brier_model,
    )
    if rep.log_loss_model >= rep.log_loss_baseline:
        raise SourceError("RROE model is no better than the constant baseline; refusing to rank")


def stage_players(ctx: Context) -> None:
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

    s = ctx.settings
    if ctx.cfbd is None:
        raise SourceError("CFBD client disabled; player plays unavailable")
    assert ctx.teams is not None
    if not ctx.season_games:
        ctx.season_games = load_season_games(ctx)
    now = s.now()
    weeks = sorted({g.week for g in ctx.season_games if g.start_date < now} | {ctx.week})
    p_rows: list[dict[str, Any]] = []
    r_rows: list[dict[str, Any]] = []
    for wk in weeks:
        completed = wk < ctx.week
        p_rows += ctx.cfbd.passing_plays(s.season, wk, s.season_type, completed=completed)
        r_rows += ctx.cfbd.rushing_plays(s.season, wk, s.season_type, completed=completed)
    if not p_rows and not r_rows:
        raise SourceError("CFBD passing/rushing plays returned nothing")
    ctx.roster = roster_index(ctx.cfbd.roster(s.season))
    passing = passing_frame(p_rows, ctx.teams)
    rushing = rushing_frame(r_rows, ctx.teams)
    tg, pg, rg = (
        target_games(passing),
        passer_games(passing, rushing),
        rusher_games(rushing, passing),
    )
    team_games = team_game_counts([passing, rushing])
    order = ctx.game_order()
    win = s.metrics.recent_games_window
    team_att = {
        int(r["team_id"]): int(r["n"])
        for r in passing.group_by("team_id").agg(pl.len().alias("n")).to_dicts()
    }
    ctx.players["team_attempts"] = pl.DataFrame(
        {"team_id": list(team_att.keys()), "team_attempts": list(team_att.values())},
        schema={"team_id": pl.Int64, "team_attempts": pl.Int64},
    )
    ctx.players["targets"] = season_table(
        tg,
        "targets",
        [
            "targets",
            "receptions",
            "rec_yards",
            "rec_td",
            "air_yards",
            "air_yards_n",
            "yac",
            "ppa",
            "rz_targets",
            "team_db",
        ],
        order,
        team_games,
        win,
    )
    ctx.players["passing"] = season_table(
        pg,
        "attempts",
        [
            "attempts",
            "completions",
            "pass_yards",
            "pass_td",
            "ints",
            "air_yards",
            "air_yards_n",
            "ppa",
            "sacks",
            "dropbacks",
            "rush_att",
            "rush_yards",
        ],
        order,
        team_games,
        win,
    )
    ctx.players["rushing"] = season_table(
        rg,
        "carries",
        [
            "carries",
            "rush_yards",
            "rush_td",
            "successes",
            "ppa",
            "targets",
            "receptions",
            "rec_yards",
        ],
        order,
        team_games,
        win,
    )
    ctx.result.sources_used.append(
        f"cfbd:/passing/plays+/rushing/plays+/roster({passing['game_id'].n_unique()} games)"
    )
    log.info(
        "stage.players",
        receivers=ctx.players["targets"].height,
        passers=ctx.players["passing"].height,
        rushers=ctx.players["rushing"].height,
    )


STAGE_FUNCS = {
    "slates": stage_slates,
    "vegas": stage_vegas,
    "weather": stage_weather,
    "pace": stage_pace,
    "epa": stage_epa,
    "rroe": stage_rroe,
    "players": stage_players,
}


# -- reload previous outputs for stages not run this time -----------------------------------

PACE_MAP = {"Plays/min (neutral)": "plays_per_min", "Plays/min L3": "plays_per_min_l3"}
RANK_KEYS = {"plays_per_min": "pace_rank", "plays_per_min_l3": "pace_rank_l3"}
EPA_MAP = {
    "Off EPA/DB": "off_epa_db",
    "Off EPA/Rush": "off_epa_rush",
    "Def EPA/DB": "def_epa_db",
    "Def EPA/Rush": "def_epa_rush",
    "Off EPA/DB L3": "off_epa_db_l3",
    "Off EPA/Rush L3": "off_epa_rush_l3",
    "Def EPA/DB L3": "def_epa_db_l3",
    "Def EPA/Rush L3": "def_epa_rush_l3",
}
RROE_MAP = {"RROE%": "rroe"}


def sheet_rows_to_metrics(
    header: list[str], rows: list[list[Any]], mapping: dict[str, str]
) -> pl.DataFrame | None:
    """Rebuild a per-team metrics frame from a raw tab. A "Rk"/"Rank" column directly
    after a mapped metric becomes `<key>_rank`."""
    cols: dict[int, str] = {}
    for i, h in enumerate(header):
        if h in mapping:
            cols[i] = mapping[h]
            if i + 1 < len(header) and header[i + 1] in ("Rk", "Rank", "Rank L3"):
                cols[i + 1] = RANK_KEYS.get(mapping[h], mapping[h] + "_rank")
    recs: list[dict[str, Any]] = []
    for r in rows:
        try:
            tid = int(r[0])
        except (TypeError, ValueError, IndexError):
            continue
        rec: dict[str, Any] = {"team_id": tid}
        for i, key in cols.items():
            v = r[i] if i < len(r) else None
            rec[key] = v if isinstance(v, int | float) else None
        recs.append(rec)
    return pl.DataFrame(recs) if recs else None


def reload_previous(
    ctx: Context, writer: SheetWriter, specs: dict[str, TabSpec], wanted: list[str]
) -> None:
    """For metric stages not requested this run, keep the main tab's columns populated
    from the last written raw tabs instead of blanking them."""
    from cfb_dfs.transform.weather import GameWeather

    todo = [
        ("pace", "pace_raw", PACE_MAP),
        ("epa", "epa_raw", EPA_MAP),
        ("rroe", "rroe_raw", RROE_MAP),
    ]
    for stage, spec_key, mapping in todo:
        if stage in wanted:
            continue
        spec = specs[spec_key]
        rows = writer.read_table(spec)
        df = sheet_rows_to_metrics(list(spec.header), rows, mapping)
        if df is not None:
            setattr(ctx, stage, df)
            log.info("pipeline.reused_previous", stage=stage, teams=df.height)
    if "vegas" not in wanted:
        spec = specs["vegas_raw"]
        vidx = {h: i for i, h in enumerate(spec.header)}
        for r in writer.read_table(spec):
            try:
                if (
                    int(r[vidx["Week"]]) != ctx.week
                    or int(r[vidx["Season"]]) != ctx.settings.season
                ):
                    continue
                gid, tid, oid = (
                    int(r[vidx["Game id"]]),
                    int(r[vidx["Team id"]]),
                    int(r[vidx["Opp id"]]),
                )
            except (TypeError, ValueError, IndexError):
                continue
            tl = TeamLine(
                gid,
                tid,
                oid,
                _cell(r, vidx, "H/A") == "H",
                _cell(r, vidx, "Book"),
                _cell(r, vidx, "Total"),
                _cell(r, vidx, "Spread"),
                _cell(r, vidx, "ITT"),
                _cell(r, vidx, "Spread open"),
                _cell(r, vidx, "Total open"),
                _cell(r, vidx, "Moneyline"),
                str(_cell(r, vidx, "Books available") or ""),
            )
            ctx.lines.setdefault(gid, []).append(tl)
        if ctx.lines:
            ctx.line_source = "previous run (Vegas Raw)"
            log.info("pipeline.reused_previous", stage="vegas", games=len(ctx.lines))
    if "weather" not in wanted:
        spec = specs["weather_raw"]
        header = list(spec.header)
        idx = {h: i for i, h in enumerate(header)}
        for r in writer.read_table(spec):
            try:
                if int(r[idx["Week"]]) != ctx.week or int(r[idx["Season"]]) != ctx.settings.season:
                    continue
                gid = int(r[idx["Game id"]])
            except (TypeError, ValueError, IndexError):
                continue
            ctx.weather[gid] = GameWeather(
                gid,
                _cell(r, idx, "Venue"),
                _cell(r, idx, "Dome") == "Y",
                temp_f=_cell(r, idx, "Temp °F (kickoff)"),
                precip_prob=_cell(r, idx, "Precip % (max, game window)"),
                wind_mph=_cell(r, idx, "Wind mph (mean)"),
                condition=_cell(r, idx, "Condition"),
                note=_cell(r, idx, "Note"),
            )


def _cell(row: list[Any], idx: dict[str, int], name: str) -> Any:
    i = idx[name]
    return row[i] if i < len(row) and row[i] != "" else None


# -- tables ----------------------------------------------------------------------------------


def fmt_kick(dt: datetime, settings: Settings) -> str:
    return dt.astimezone(settings.tz).strftime("%a %m/%d %I:%M %p").replace(" 0", " ")


def _rows_by_team(df: pl.DataFrame | None) -> dict[int, dict[str, Any]]:
    if df is None:
        return {}
    return {int(r["team_id"]): r for r in df.to_dicts()}


def _team_cols(ctx: Context, team_id: int) -> list[Cell]:
    assert ctx.teams is not None
    t = ctx.teams.get(team_id)
    return [team_id, ctx.teams.abbr(team_id), t.school if t else None, t.conference if t else None]


def build_teams_table(ctx: Context, spec: TabSpec) -> TableWrite:
    assert ctx.teams is not None
    rows: list[list[Cell]] = []
    for t in sorted(ctx.teams.teams, key=lambda t: ((t.classification or "zz"), t.school)):
        if (t.classification or "").lower() not in {"fbs", "fcs"}:
            continue
        rows.append(
            [
                t.id,
                ctx.teams.abbr(t.id),
                t.school,
                t.mascot,
                t.conference,
                (t.classification or "").upper(),
                t.abbreviation,
                t.logos[0] if t.logos else None,
            ]
        )
    return TableWrite(spec, rows)


def build_pace_table(ctx: Context, spec: TabSpec) -> TableWrite:
    assert ctx.pace is not None
    n = ctx.pace.filter(pl.col("pace_rank").is_not_null()).height
    rows: list[list[Cell]] = []
    for r in ctx.pace.filter(pl.col("team_id").is_in(list(ctx.fbs_ids))).to_dicts():
        rows.append(
            [
                *_team_cols(ctx, int(r["team_id"])),
                r["games"],
                r["plays_per_min"],
                r["pace_rank"],
                r["sec_per_play"],
                r["neutral_plays"],
                _mins(r["neutral_secs"]),
                r["plays_per_min_l3"],
                r["pace_rank_l3"],
                r["games_l3"],
                r["all_plays_per_min"],
                r["all_plays"],
                _mins(r["all_secs"]),
                r["drives"],
            ]
        )
    stamp = f"{ctx.settings.now():%Y-%m-%d %H:%M %Z}"
    title = (
        f"Pace · rank of {n} FBS teams (1 = fastest) · drive-based: scrimmage plays / drive "
        f"minutes · season {ctx.settings.season} through week {ctx.week - 1} · "
        f"{describe_filters(ctx.settings.metrics)} · updated {stamp}"
    )
    return TableWrite(spec, rows, title=title)


def build_epa_table(ctx: Context, spec: TabSpec) -> TableWrite:
    assert ctx.epa is not None
    n = ctx.epa.filter(pl.col("off_epa_play_rank").is_not_null()).height
    rows: list[list[Cell]] = []
    for r in ctx.epa.filter(pl.col("team_id").is_in(list(ctx.fbs_ids))).to_dicts():
        tid = int(r["team_id"])
        ppa = ctx.ppa.get(tid, {})
        rows.append(
            [
                *_team_cols(ctx, tid),
                r["games"],
                r["off_epa_db"],
                r["off_epa_db_rank"],
                r["off_epa_rush"],
                r["off_epa_rush_rank"],
                r["off_epa_play"],
                r["off_epa_play_rank"],
                r["off_db_n"],
                r["off_rush_n"],
                r["def_epa_db"],
                r["def_epa_db_rank"],
                r["def_epa_rush"],
                r["def_epa_rush_rank"],
                r["def_epa_play"],
                r["def_epa_play_rank"],
                r["def_db_n"],
                r["def_rush_n"],
                r["off_epa_db_l3"],
                r["off_epa_db_l3_rank"],
                r["off_epa_rush_l3"],
                r["off_epa_rush_l3_rank"],
                r["def_epa_db_l3"],
                r["def_epa_db_l3_rank"],
                r["def_epa_rush_l3"],
                r["def_epa_rush_l3_rank"],
                ppa.get("off_pass"),
                ppa.get("off_rush"),
                ppa.get("def_pass"),
                ppa.get("def_rush"),
            ]
        )
    stamp = f"{ctx.settings.now():%Y-%m-%d %H:%M %Z}"
    title = (
        f"EPA/play · rank of {n} FBS teams (offense 1 = best, defense 1 = fewest allowed) · "
        "EPA = CFBD PPA per play; dropback = pass attempts + sacks; rush = designed rushes · "
        f"{describe_filters(ctx.settings.metrics)} · CFBD PPA columns = CFBD's own season "
        f"aggregate (excludeGarbageTime=true) for comparison · updated {stamp}"
    )
    return TableWrite(spec, rows, title=title)


def build_weather_table(ctx: Context, spec: TabSpec) -> TableWrite:
    assert ctx.teams is not None
    s = ctx.settings
    now = s.now().strftime("%Y-%m-%d %H:%M %Z")
    rows: list[list[Cell]] = []
    by_id = {gs.game.id: gs.game for gs in ctx.slates} or {g.id: g for g in ctx.games}
    for gid, w in ctx.weather.items():
        g = by_id.get(gid)
        if g is None:
            continue
        rows.append(
            [
                s.season,
                ctx.week,
                gid,
                fmt_kick(g.start_date, s),
                ctx.teams.abbr(g.away_id),
                ctx.teams.abbr(g.home_id),
                w.venue,
                w.city,
                w.state,
                "Y" if w.dome else "",
                w.temp_f,
                w.precip_prob,
                w.precip_in,
                w.wind_mph,
                w.gust_mph,
                w.condition,
                w.note,
                now,
            ]
        )
    rows.sort(key=lambda r: str(r[3]))
    return TableWrite(spec, rows)


def build_rroe_table(ctx: Context, spec: TabSpec) -> TableWrite:
    assert ctx.rroe is not None
    rep = ctx.rroe_report
    cfg = ctx.settings.metrics.rroe
    n = ctx.rroe.filter(pl.col("rroe_rank").is_not_null()).height
    rows: list[list[Cell]] = []
    for r in ctx.rroe.filter(pl.col("team_id").is_in(list(ctx.fbs_ids))).to_dicts():
        note = "low sample" if (r["plays"] or 0) < cfg.min_plays else None
        rows.append(
            [
                *_team_cols(ctx, int(r["team_id"])),
                r["games"],
                r["plays"],
                r["rush_rate"],
                r["exp_rush_rate"],
                r["rroe"],
                r["rroe_rank"],
                r["rroe_l3"],
                r["rroe_rank_l3"],
                r["plays_l3"],
                note,
            ]
        )
    title = (
        f"RROE% = (actual rushes - expected rushes) / plays x 100 on 1st/2nd down, garbage time "
        f"excluded · rank of {n} FBS teams (1 = most run-heavy vs expectation) · model: gradient "
        f"boosting on down/distance/yards-to-goal/score/clock/timeouts/spread/total, trained on "
        f"{rep.seasons if rep else '?'} ({rep.n_train if rep else '?'} plays), holdout log loss "
        f"{rep.log_loss_model if rep else '?'} vs baseline {rep.log_loss_baseline if rep else '?'} "
        f"· see reports/rroe_model_report.md · updated {ctx.settings.now():%Y-%m-%d %H:%M %Z}"
    )
    return TableWrite(spec, rows, title=title)


def _weeks(r: dict[str, Any]) -> list[Cell]:
    return [r.get(f"wk{w}") for w in range(1, 17)]


def _player_ident(ctx: Context, r: dict[str, Any]) -> list[Cell]:
    assert ctx.teams is not None
    ro = ctx.roster.get(str(r["player_id"]), {})
    tid = int(r["team_id"])
    return [
        r["player_id"],
        ctx.teams.abbr(tid),
        ro.get("name") or r.get("name"),
        ro.get("position"),
    ]


def _r(num: Any, den: Any, digits: int = 2) -> Cell:
    if num is None or not den:
        return None
    return round(num / den, digits)


def build_targets_table(ctx: Context, spec: TabSpec) -> TableWrite:
    df = ctx.players["targets"].join(ctx.players["team_attempts"], on="team_id", how="left")
    rows: list[list[Cell]] = []
    for r in df.to_dicts():
        rows.append(
            [
                *_player_ident(ctx, r),
                r.get("team_games"),
                r["games"],
                r["targets"],
                _r(r["targets"], r["games"]),
                r["targets_l3"],
                _r(r["targets_l3"], r["games_l3"]),
                _r(100 * r["targets"], r.get("team_attempts"), 1),
                r["receptions"],
                r["rec_yards"],
                r["rec_td"],
                _r(r["air_yards"], r["air_yards_n"], 1),
                r["air_yards"],
                r["yac"],
                r["rz_targets"],
                _r(r["ppa"], r["targets"], 3),
                r["team_db"],
                _r(r["targets"], r["team_db"], 3),
                *_weeks(r),
            ]
        )
    return TableWrite(
        spec,
        rows,
        title=_players_title(
            ctx,
            "Targets = pass attempts with a "
            "named receiver (throwaways/spikes excluded); Tgt share = targets / team pass "
            "attempts; RZ = target inside the 20; routes are not published by any free source, so "
            "Tgt/team DB (targets per team dropback in the player's games) is the closest proxy "
            "to targets per route run",
        ),
    )


def build_passing_table(ctx: Context, spec: TabSpec) -> TableWrite:
    rows: list[list[Cell]] = []
    for r in ctx.players["passing"].to_dicts():
        rows.append(
            [
                *_player_ident(ctx, r),
                r.get("team_games"),
                r["games"],
                r["dropbacks"],
                r["attempts"],
                _r(r["attempts"], r["games"]),
                r["attempts_l3"],
                _r(r["attempts_l3"], r["games_l3"]),
                r["completions"],
                _r(100 * r["completions"], r["attempts"], 1),
                r["pass_yards"],
                _r(r["pass_yards"], r["attempts"]),
                _r(r["air_yards"], r["air_yards_n"], 1),
                r["pass_td"],
                r["ints"],
                r["sacks"],
                _r(r["ppa"], r["attempts"], 3),
                r["rush_att"],
                r["rush_yards"],
                *_weeks(r),
            ]
        )
    return TableWrite(
        spec,
        rows,
        title=_players_title(
            ctx,
            "Attempts exclude spikes; dropbacks = "
            "attempts + sacks taken; designed runs = individually attributed rushes by "
            "the passer (scrambles cannot be separated)",
        ),
    )


def build_rushing_table(ctx: Context, spec: TabSpec) -> TableWrite:
    rows: list[list[Cell]] = []
    for r in ctx.players["rushing"].to_dicts():
        rows.append(
            [
                *_player_ident(ctx, r),
                r.get("team_games"),
                r["games"],
                r["carries"],
                _r(r["carries"], r["games"]),
                r["carries_l3"],
                _r(r["carries_l3"], r["games_l3"]),
                r["rush_yards"],
                _r(r["rush_yards"], r["carries"]),
                r["rush_td"],
                _r(100 * r["successes"], r["carries"], 1),
                _r(r["ppa"], r["carries"], 3),
                r["targets"],
                r["receptions"],
                r["rec_yards"],
                r["carries"] + r["receptions"],
                *_weeks(r),
            ]
        )
    return TableWrite(
        spec,
        rows,
        title=_players_title(
            ctx,
            "Carries = individually attributed "
            "rushes only (no sacks, kneels, team rushes); success = CFBD success flag",
        ),
    )


def _players_title(ctx: Context, definition: str) -> str:
    return (
        f"Season {ctx.settings.season} through week {ctx.week - 1} · source CFBD /passing/plays, "
        f"/rushing/plays, /roster · {definition} · L3 = last "
        f"{ctx.settings.metrics.recent_games_window} team games · updated "
        f"{ctx.settings.now():%Y-%m-%d %H:%M %Z}"
    )


def _mins(secs: Any) -> Cell:
    return round(secs / 60, 1) if secs else None


def build_vegas_rows(ctx: Context) -> list[list[Cell]]:
    assert ctx.teams is not None
    s = ctx.settings
    now = s.now().strftime("%Y-%m-%d %H:%M %Z")
    slate_by_game = {gs.game.id: gs for gs in ctx.slates}
    rows: list[list[Cell]] = []
    for g in sorted(ctx.games, key=lambda g: (g.start_date, g.id)):
        pair = ctx.lines.get(g.id)
        gs = slate_by_game.get(g.id)
        for tl in pair or _blank_lines(g):
            team = ctx.teams.get(tl.team_id)
            rows.append(
                [
                    s.season,
                    ctx.week,
                    g.id,
                    fmt_kick(g.start_date, s),
                    tl.team_id,
                    ctx.teams.abbr(tl.team_id),
                    team.school if team else None,
                    tl.opponent_id,
                    ctx.teams.abbr(tl.opponent_id),
                    "H" if tl.is_home else "A",
                    tl.book,
                    tl.total,
                    tl.spread,
                    tl.implied_total,
                    tl.spread_open,
                    tl.total_open,
                    tl.moneyline,
                    tl.books_available,
                    gs.label_text if gs else "",
                    now,
                ]
            )
    return rows


def _blank_lines(g: Game) -> list[TeamLine]:
    return [
        TeamLine(
            g.id, g.home_id, g.away_id, True, None, None, None, None, None, None, None, "none"
        ),
        TeamLine(
            g.id, g.away_id, g.home_id, False, None, None, None, None, None, None, None, "none"
        ),
    ]


def merge_vegas_history(
    existing: list[list[Any]], new_rows: list[list[Cell]], week: int, season: int
) -> list[list[Cell]]:
    """Keep other weeks' rows from the sheet, replace the current week's."""
    kept: list[list[Cell]] = []
    for row in existing:
        if len(row) < 2:
            continue
        try:
            r_season, r_week = int(row[0]), int(row[1])
        except (TypeError, ValueError):
            continue
        if r_season == season and r_week == week:
            continue
        kept.append(list(row))
    return sorted(
        [*kept, *new_rows], key=lambda r: (int(r[0] or 0), int(r[1] or 0), str(r[3] or ""))
    )


def build_main_rows(ctx: Context) -> list[list[Cell]]:
    assert ctx.teams is not None
    s = ctx.settings
    pace = _rows_by_team(ctx.pace)
    epa = _rows_by_team(ctx.epa)
    rroe = _rows_by_team(ctx.rroe)
    rows: list[list[Cell]] = []
    for gs in ctx.slates:
        g = gs.game
        pair = ctx.lines.get(g.id) or _blank_lines(g)
        away = next(t for t in pair if not t.is_home)
        home = next(t for t in pair if t.is_home)
        for tl in (away, home):
            tid = tl.team_id
            team = ctx.teams.get(tid)
            is_fbs = ctx.teams.is_fbs(tid)
            p = pace.get(tid, {})
            e = epa.get(tid, {})
            rr = rroe.get(tid, {})
            w = ctx.weather.get(g.id)
            na: Cell = None if is_fbs else "FCS"

            def rk(d: dict[str, Any], key: str, fill: Cell = na) -> Cell:
                v = d.get(key)
                return v if v is not None else fill

            row: list[Cell] = [
                gs.primary,
                fmt_kick(g.start_date, s),
                ctx.teams.abbr(tid),
                tl.total,
                tl.spread,
                tl.implied_total,
                w.temp_f if w else None,
                w.precip_prob if w else None,
                w.wind_mph if w else None,
                (w.condition or w.note) if w else None,
                p.get("plays_per_min"),
                rk(p, "pace_rank"),
                e.get("off_epa_db"),
                rk(e, "off_epa_db_rank"),
                e.get("off_epa_rush"),
                rk(e, "off_epa_rush_rank"),
                rk(rr, "rroe", None),
                rk(rr, "rroe_rank"),
                e.get("def_epa_db"),
                rk(e, "def_epa_db_rank"),
                e.get("def_epa_rush"),
                rk(e, "def_epa_rush_rank"),
                p.get("plays_per_min_l3"),
                e.get("off_epa_db_l3"),
                e.get("off_epa_rush_l3"),
                e.get("def_epa_db_l3"),
                e.get("def_epa_rush_l3"),
                gs.label_text,
                tl.book,
                team.school if team else None,
                team.conference if team else None,
                (team.classification or "").upper() if team else None,
                g.id,
                gs.source,
            ]
            assert len(row) == MAIN_NCOLS
            rows.append(row)
        rows.append([None] * MAIN_NCOLS)
    if rows:
        rows.pop()
    return rows


def main_title(ctx: Context) -> str:
    s = ctx.settings
    r = ctx.result
    parts = [
        f"Last updated: {s.now():%Y-%m-%d %I:%M %p %Z}",
        r.status,
        f"week {ctx.week}",
        f"slates: {ctx.slate_source} ({len({gs.primary for gs in ctx.slates})})",
        f"lines: {ctx.line_source}",
    ]
    parts.append(f"refreshed: {', '.join(r.stages_ok)}")
    if ctx.pace is not None:
        parts.append(f"pace/EPA/RROE through week {ctx.week - 1}")
    if ctx.weather:
        parts.append("weather: Open-Meteo at kickoff")
    if r.warnings:
        parts.append(f"{len(r.warnings)} warning(s), see Log tab")
    return " · ".join(parts)


# -- orchestration ---------------------------------------------------------------------------


def run_pipeline(
    settings: Settings, secrets: Secrets, stages: list[str] | None, dry_run: bool, refresh: bool
) -> int:
    from cfb_dfs.cli import _cfbd, detect_week
    from cfb_dfs.sheets.auth import load_credentials
    from cfb_dfs.sheets.client import SheetsClient
    from cfb_dfs.sheets.log import append_log, log_row

    started = time.monotonic()
    wanted = list(stages or ALL_STAGES)
    unknown = sorted(set(wanted) - set(ALL_STAGES))
    if unknown:
        log.error("pipeline.unknown_stages", unknown=unknown, allowed=ALL_STAGES)
        return 2
    if "slates" not in wanted:
        wanted.insert(0, "slates")  # the main tab always needs slates

    cache = DiskCache(settings.cache.dir, refresh=refresh)
    cfbd = _cfbd(settings, secrets, refresh=refresh) if settings.sources.cfbd.enabled else None
    week, week_source = detect_week(settings, cfbd)
    result = RunResult(week=week)
    ctx = Context(settings, secrets, cache, cfbd, week, result)
    log.info(
        "pipeline.start",
        season=settings.season,
        week=week,
        week_source=week_source,
        stages=wanted,
        dry_run=dry_run,
        refresh=refresh,
    )

    if not secrets.sheet_id:
        log.error("pipeline.no_sheet_id")
        return 2
    writer = SheetWriter(SheetsClient(load_credentials(secrets), secrets.sheet_id))
    specs = build_specs(settings.sheet.tabs)

    try:
        ctx.teams = load_teams(ctx)
        ctx.games = load_games(ctx)
        ctx.config_rows = writer.read_table(specs["config"])
    except SourceError as exc:
        log.error("pipeline.foundation_failed", error=str(exc))
        result.stages_failed["foundation"] = str(exc)
        _finish(ctx, writer, specs, started, dry_run, append_log, log_row, tables=[])
        return result.exit_code

    for stage in wanted:
        if stage not in IMPLEMENTED:
            result.warn(f"stage {stage} not implemented yet")
            continue
        try:
            STAGE_FUNCS[stage](ctx)
            result.stages_ok.append(stage)
        except SourceError as exc:
            result.stages_failed[stage] = str(exc)
            log.error("stage.failed", stage=stage, error=str(exc))
        except Exception as exc:  # keep other stages alive, but surface loudly
            result.stages_failed[stage] = f"{exc.__class__.__name__}: {exc}"
            log.exception("stage.crashed", stage=stage)

    if not ctx.slates and ctx.games:  # slates stage failed: still render every game
        assigned, _ = assign(
            ctx.games, [], [], settings.slates.kickoff_windows_ct, ctx.teams, settings.tz
        )
        ctx.slates = assigned
        ctx.slate_source = "kickoff-window (fallback)"

    try:
        reload_previous(ctx, writer, specs, wanted)
    except Exception as exc:  # cosmetic continuity only
        result.warn(f"could not reload previous metrics: {exc}")

    tables: list[TableWrite] = [build_teams_table(ctx, specs["teams"])]
    if "vegas" in result.stages_ok:
        existing = writer.read_table(specs["vegas_raw"])
        merged = merge_vegas_history(existing, build_vegas_rows(ctx), week, settings.season)
        tables.append(TableWrite(specs["vegas_raw"], merged))
    if "pace" in result.stages_ok:
        tables.append(build_pace_table(ctx, specs["pace_raw"]))
    if "epa" in result.stages_ok:
        tables.append(build_epa_table(ctx, specs["epa_raw"]))
    if "weather" in result.stages_ok:
        tables.append(build_weather_table(ctx, specs["weather_raw"]))
    if "rroe" in result.stages_ok:
        tables.append(build_rroe_table(ctx, specs["rroe_raw"]))
    if "players" in result.stages_ok:
        tables.append(build_targets_table(ctx, specs["targets_raw"]))
        tables.append(build_passing_table(ctx, specs["passing_raw"]))
        tables.append(build_rushing_table(ctx, specs["rushing_raw"]))
    if "vegas" in wanted and "vegas" not in result.stages_ok:
        # Lines failed: keep the previous main tab rather than blanking it.
        result.warn("main tab left unchanged because the vegas stage failed")
    else:
        tables.append(TableWrite(specs["main"], build_main_rows(ctx), title=main_title(ctx)))
    _finish(ctx, writer, specs, started, dry_run, append_log, log_row, tables)
    return result.exit_code


def _finish(
    ctx: Context,
    writer: SheetWriter,
    specs: dict[str, TabSpec],
    started: float,
    dry_run: bool,
    append_log: Any,
    log_row: Any,
    tables: list[TableWrite],
) -> None:
    r = ctx.result
    s = ctx.settings
    created = writer.ensure_tabs(list(specs.values()), dry_run=dry_run)
    if created and not dry_run:
        writer.apply_properties([specs[k] for k in specs])
        if specs["config"].name in created:
            tables.append(TableWrite(specs["config"], [], title="Config"))
        if specs["log"].name in created:
            tables.append(TableWrite(specs["log"], []))
    report = writer.write_tables(tables, dry_run=dry_run)
    r.rows_written = report.rows_written
    if not dry_run and "players" in r.stages_ok:
        for disp, raw, sort_col in (
            ("targets", "targets_raw", "Targets"),
            ("passing", "passing_raw", "Attempts"),
            ("rushing", "rushing_raw", "Carries"),
        ):
            try:
                default = (
                    ctx.teams.abbr(ctx.slates[0].game.home_id) if ctx.slates and ctx.teams else None
                )
                writer.setup_display_tab(
                    specs[disp], specs[raw], sort_col, specs["teams"].name, default
                )
            except Exception as exc:
                r.warn(f"display tab {specs[disp].name} setup failed: {exc}")
    if not dry_run and "players" in r.stages_ok:
        from cfb_dfs.sheets.format import PASSING_RULES, RUSHING_RULES, TARGET_RULES

        for key, rules in (
            ("targets", TARGET_RULES),
            ("passing", PASSING_RULES),
            ("rushing", RUSHING_RULES),
        ):
            try:
                writer.format_tab(specs[key], rules, week_block=True)
            except Exception as exc:
                r.warn(f"formatting {specs[key].name} failed: {exc}")
    if not dry_run:
        for stage, key in (
            ("pace", "pace_raw"),
            ("epa", "epa_raw"),
            ("rroe", "rroe_raw"),
            ("players", "targets_raw"),
        ):
            if stage in r.stages_failed:
                msg = (
                    f"STALE: {stage} failed on {s.now():%Y-%m-%d %H:%M %Z} "
                    f"({r.stages_failed[stage][:200]}); rows below are from the last good run"
                )
                try:
                    writer.mark_stale(specs[key], msg)
                except Exception as exc:
                    log.warning("stale_mark_failed", tab=key, error=str(exc))
        try:
            writer.protect_tabs(list(specs.values()), s.sheet.protected_editor_emails)
        except Exception as exc:
            r.warn(f"protecting raw tabs failed: {exc}")
    if not dry_run and any(t.spec.name == specs["main"].name for t in tables):
        try:
            writer.format_main(specs["main"])
        except Exception as exc:  # formatting is cosmetic; never fail the run
            r.warn(f"main tab formatting failed: {exc}")
    r.duration_s = time.monotonic() - started
    if dry_run:
        for tab, summary in report.diff.items():
            log.info("dry_run.diff", tab=tab, summary=summary)
    else:
        try:
            append_log(
                writer,
                specs["log"],
                log_row(
                    s.now(),
                    s.season,
                    ctx.week,
                    r.status,
                    r.stages_ok,
                    r.stages_failed,
                    r.rows_written,
                    r.sources_used,
                    r.warnings,
                    r.duration_s,
                    ctx.cfbd.calls_made if ctx.cfbd else 0,
                    dry_run,
                ),
            )
        except Exception:  # logging must never fail the run
            log.exception("log_tab.append_failed")
    if ctx.cfbd is not None and not dry_run and len(r.stages_ok) >= 4:
        try:
            info = ctx.cfbd.info()
            remaining = int(info.get("remainingCalls") or 0)
            log.info("cfbd.budget", remaining=remaining, limit=info.get("monthlyLimit"))
            if remaining < 100:
                r.warn(f"CFBD budget low: {remaining} calls left this month")
        except Exception as exc:
            log.warning("cfbd.budget_check_failed", error=str(exc))
    log.info(
        "pipeline.done",
        status=r.status,
        ok=r.stages_ok,
        failed=r.stages_failed,
        rows=r.rows_written,
        warnings=len(r.warnings),
        duration_s=round(r.duration_s, 1),
        cfbd_calls=ctx.cfbd.calls_made if ctx.cfbd else 0,
        sheet_reads=writer.client.reads,
        sheet_writes=writer.client.writes,
    )
