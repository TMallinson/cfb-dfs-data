"""Pipeline orchestration.

Contract: each stage fetches + transforms and records its outcome; a stage
failure is logged and the remaining stages still run; the sheet is written
once at the end (or diffed on --dry-run); exit code is non-zero if any stage
failed. Stages land phase by phase: slates, vegas (Phase 3); pace, epa
(Phase 4); rroe (Phase 5); players (Phase 6).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from cfb_dfs.config import Secrets, Settings
from cfb_dfs.logging_setup import get_logger
from cfb_dfs.models import Game, GameLines, Team
from cfb_dfs.models.slates import Slate
from cfb_dfs.sheets.layout import TabSpec, build_specs
from cfb_dfs.sheets.writer import Cell, SheetWriter, TableWrite
from cfb_dfs.sources.cache import DiskCache
from cfb_dfs.sources.cfbd import CfbdClient
from cfb_dfs.sources.http import SourceError
from cfb_dfs.transform.slates import GameSlates, assign, manual_slates_from_rows
from cfb_dfs.transform.teams import Overrides, TeamIndex
from cfb_dfs.transform.vegas import TeamLine, all_team_lines

log = get_logger(__name__)

ALL_STAGES = ["slates", "vegas", "pace", "epa", "rroe", "players"]
IMPLEMENTED = {"slates", "vegas"}
OVERRIDES_PATH = "data/overrides/team_aliases.yaml"


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
    slates: list[GameSlates] = field(default_factory=list)
    dk_slates: list[Slate] = field(default_factory=list)
    lines: dict[int, list[TeamLine]] = field(default_factory=dict)
    line_source: str = "none"
    slate_source: str = "none"
    config_rows: list[list[Any]] = field(default_factory=list)


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
            pairs = [
                (t.id, g.away.abbreviation)
                for sl in dk
                for g in sl.games
                if (t := ctx.teams.match_dk(g.away)) is not None
            ] + [
                (t.id, g.home.abbreviation)
                for sl in dk
                for g in sl.games
                if (t := ctx.teams.match_dk(g.home)) is not None
            ]
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


STAGE_FUNCS = {"slates": stage_slates, "vegas": stage_vegas}


# -- tables ----------------------------------------------------------------------------------


def fmt_kick(dt: datetime, settings: Settings) -> str:
    return dt.astimezone(settings.tz).strftime("%a %m/%d %I:%M %p").replace(" 0", " ")


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
            opp = ctx.teams.get(tl.opponent_id)
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
            if opp is None:
                ctx.result.warn(f"Unknown opponent id {tl.opponent_id} in game {g.id}")
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
    rows: list[list[Cell]] = []
    ncols = 32
    for gs in ctx.slates:
        g = gs.game
        pair = ctx.lines.get(g.id) or _blank_lines(g)
        away = next(t for t in pair if not t.is_home)
        home = next(t for t in pair if t.is_home)
        for tl in (away, home):
            team = ctx.teams.get(tl.team_id)
            is_fbs = ctx.teams.is_fbs(tl.team_id)
            blank_rank: Cell = None if is_fbs else "FCS"
            row: list[Cell] = [
                gs.primary,
                fmt_kick(g.start_date, s),
                ctx.teams.abbr(tl.team_id),
                ctx.teams.abbr(tl.opponent_id),
                "H" if tl.is_home else "A",
                tl.total,
                tl.spread,
                tl.implied_total,
                None,
                blank_rank,  # pace, rank            (Phase 4)
                None,
                blank_rank,
                None,
                blank_rank,  # off EPA/DB, rk, off EPA/rush, rk (Phase 4)
                None,
                blank_rank,  # RROE, rk               (Phase 5)
                None,
                blank_rank,
                None,
                blank_rank,  # def EPA/DB, rk, def EPA/rush, rk (Phase 4)
                None,
                None,
                None,
                None,
                None,  # last-3 columns (Phase 4)
                gs.label_text,
                tl.book,
                team.school if team else None,
                team.conference if team else None,
                (team.classification or "").upper() if team else None,
                g.id,
                gs.source,
            ]
            assert len(row) == ncols
            rows.append(row)
        rows.append([None] * ncols)
    if rows:
        rows.pop()  # no trailing spacer
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
    if "vegas" in wanted and "slates" not in wanted:
        wanted.insert(0, "slates")  # the main tab needs both

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

    # Foundation: teams + games + config rows. Without these nothing can be written.
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

    tables: list[TableWrite] = [build_teams_table(ctx, specs["teams"])]
    if "vegas" in result.stages_ok or "slates" in result.stages_ok:
        existing = writer.read_table(specs["vegas_raw"])
        merged = merge_vegas_history(existing, build_vegas_rows(ctx), week, settings.season)
        tables.append(TableWrite(specs["vegas_raw"], merged))
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
