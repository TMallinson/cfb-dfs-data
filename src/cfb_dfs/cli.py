"""Command line interface.

python -m cfb_dfs run --week 2 [--dry-run] [--only vegas,pace] [--refresh] [--sheet-id ID]
python -m cfb_dfs smoke            # live checks: CFBD auth + calendar, Sheets auth + title
python -m cfb_dfs inspect-sheet    # read-only structure dump -> reports/sheet_structure.md
python -m cfb_dfs week             # print the auto-detected week
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from cfb_dfs.config import Secrets, Settings, load_settings
from cfb_dfs.logging_setup import configure_logging, get_logger
from cfb_dfs.sources.cache import DiskCache
from cfb_dfs.sources.cfbd import CfbdClient
from cfb_dfs.week import fallback_week, resolve_week

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)
log = get_logger("cfb_dfs.cli")

ConfigOpt = Annotated[Path, typer.Option("--config", help="Path to config.yaml")]


def _bootstrap(config: Path, verbose: bool = False) -> tuple[Settings, Secrets]:
    configure_logging(verbose)
    return load_settings(config), Secrets.from_env()


def _cfbd(settings: Settings, secrets: Secrets, refresh: bool = False) -> CfbdClient:
    if not secrets.cfbd_api_key:
        raise typer.BadParameter("CFBD_API_KEY is not set")
    cache = DiskCache(settings.cache.dir, refresh=refresh)
    return CfbdClient(secrets.cfbd_api_key, cache, settings.cache.ttl_hours, settings.sources.cfbd)


def detect_week(settings: Settings, cfbd: CfbdClient | None) -> tuple[int, str]:
    if settings.week is not None:
        return settings.week, "config"
    now = settings.now()
    if cfbd is not None:
        try:
            week = resolve_week(cfbd.calendar(settings.season), now, settings.season_type)
            if week is not None:
                return week, "cfbd calendar"
        except Exception as exc:  # fall back, never block on week detection
            log.warning("week.calendar_failed", error=str(exc))
    return fallback_week(now, settings.season), "date heuristic"


@app.command()
def week(config: ConfigOpt = Path("config.yaml")) -> None:
    """Print the auto-detected season week."""
    settings, secrets = _bootstrap(config)
    cfbd = _cfbd(settings, secrets) if secrets.cfbd_api_key else None
    wk, source = detect_week(settings, cfbd)
    now = f"{settings.now():%Y-%m-%d %H:%M %Z}"
    typer.echo(f"season={settings.season} week={wk} (source: {source}, now={now})")


@app.command()
def smoke(config: ConfigOpt = Path("config.yaml")) -> None:
    """Live smoke test of every credentialed dependency. Exits non-zero on failure."""
    settings, secrets = _bootstrap(config)
    failures = 0

    # CFBD ---------------------------------------------------------------------------------
    try:
        cfbd = _cfbd(settings, secrets)
        info = cfbd.info()
        wk, source = detect_week(settings, cfbd)
        typer.echo(
            f"[ok] CFBD: tier={info.get('tierName')} remaining={info.get('remainingCalls')}/"
            f"{info.get('monthlyLimit')} resets={info.get('resetAt')} | week {wk} via {source}"
        )
    except Exception as exc:
        failures += 1
        typer.echo(f"[FAIL] CFBD: {exc}")

    # Sheets -------------------------------------------------------------------------------
    try:
        from cfb_dfs.sheets.auth import load_credentials
        from cfb_dfs.sheets.client import SheetsClient

        if not secrets.sheet_id:
            raise RuntimeError("SHEET_ID is not set")
        creds = load_credentials(secrets)
        client = SheetsClient(creds, secrets.sheet_id)
        meta = client.get_metadata(fields="properties(title,timeZone),sheets(properties(title))")
        auth_mode = "b64" if secrets.google_service_account_b64 else "file"
        typer.echo(
            f"[ok] Sheets: '{meta['properties']['title']}' tz={meta['properties'].get('timeZone')} "
            f"tabs={len(meta.get('sheets', []))} auth={auth_mode}"
        )
    except Exception as exc:
        failures += 1
        typer.echo(f"[FAIL] Sheets: {exc}")

    raise typer.Exit(code=1 if failures else 0)


@app.command("inspect-sheet")
def inspect_sheet(
    config: ConfigOpt = Path("config.yaml"),
    sheet_id: Annotated[str | None, typer.Option("--sheet-id")] = None,
    out: Annotated[Path, typer.Option("--out")] = Path("reports/sheet_structure.md"),
) -> None:
    """Read-only dump of tab names, headers, formulas, validations, protections."""
    from cfb_dfs.sheets.auth import load_credentials
    from cfb_dfs.sheets.client import SheetsClient
    from cfb_dfs.sheets.inspect import inspect_spreadsheet

    _settings, secrets = _bootstrap(config)
    sid = sheet_id or secrets.sheet_id
    if not sid:
        raise typer.BadParameter("SHEET_ID is not set and --sheet-id not given")
    client = SheetsClient(load_credentials(secrets), sid)
    _, report = inspect_spreadsheet(client)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    typer.echo(f"wrote {out} ({client.reads} read requests)")


@app.command("reset-sheet")
def reset_sheet(
    config: ConfigOpt = Path("config.yaml"),
    sheet_id: Annotated[str | None, typer.Option("--sheet-id")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="actually delete; otherwise dry-run")] = False,
) -> None:
    """One-time: create the pipeline's tabs, then DELETE every other tab in the sheet."""
    from cfb_dfs.sheets.auth import load_credentials
    from cfb_dfs.sheets.client import SheetsClient
    from cfb_dfs.sheets.layout import build_specs
    from cfb_dfs.sheets.writer import SheetWriter, TableWrite

    settings, secrets = _bootstrap(config)
    sid = sheet_id or secrets.sheet_id
    if not sid:
        raise typer.BadParameter("SHEET_ID is not set and --sheet-id not given")
    writer = SheetWriter(SheetsClient(load_credentials(secrets), sid))
    specs = build_specs(settings.sheet.tabs)
    keep = {s.name for s in specs.values()}
    doomed = [t for t in writer.sheet_ids() if t not in keep]
    typer.echo(f"keep: {sorted(keep)}")
    typer.echo(f"delete ({len(doomed)}): {doomed}")
    if not yes:
        typer.echo("dry run; pass --yes to apply")
        return
    created = writer.ensure_tabs(list(specs.values()))
    writer.apply_properties(list(specs.values()))
    tables = [
        TableWrite(specs[k], [], title=("Config" if k == "config" else None))
        for k in specs
        if specs[k].name in created
    ]
    writer.write_tables(tables)
    deleted = writer.delete_tabs(doomed)
    typer.echo(f"created {created}; deleted {len(deleted)} tab(s)")


@app.command()
def run(
    config: ConfigOpt = Path("config.yaml"),
    week: Annotated[int | None, typer.Option("--week")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    only: Annotated[
        str | None, typer.Option("--only", help="comma list: vegas,slates,pace,epa,rroe,players")
    ] = None,
    refresh: Annotated[bool, typer.Option("--refresh", help="bypass the disk cache")] = False,
    sheet_id: Annotated[str | None, typer.Option("--sheet-id")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
    force: Annotated[
        bool, typer.Option("--force", help="run even outside the season window")
    ] = False,
) -> None:
    """Run the pipeline: fetch sources, compute metrics, write the sheet."""
    settings, secrets = _bootstrap(config, verbose)
    if week is not None:
        settings.week = week
    if sheet_id:
        secrets.sheet_id = sheet_id
    from cfb_dfs.pipeline import run_pipeline

    win = settings.season_window
    today = settings.now().date()
    if win is not None and not (win.start <= today <= win.end) and not force:
        typer.echo(f"outside season window {win.start}..{win.end}; nothing to do (use --force)")
        raise typer.Exit(code=0)
    stages = [s.strip() for s in only.split(",")] if only else None
    code = run_pipeline(settings, secrets, stages=stages, dry_run=dry_run, refresh=refresh)
    raise typer.Exit(code=code)
