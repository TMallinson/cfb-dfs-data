"""Hidden Log tab: one row per run."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime

from cfb_dfs.sheets.layout import TabSpec
from cfb_dfs.sheets.writer import Cell, SheetWriter


def git_short_sha() -> str:
    sha = os.environ.get("GITHUB_SHA")
    if sha:
        return sha[:7]
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def log_row(
    now: datetime,
    season: int,
    week: int,
    status: str,
    stages_ok: list[str],
    stages_failed: dict[str, str],
    rows_written: dict[str, int],
    sources: list[str],
    warnings: list[str],
    duration_s: float,
    cfbd_calls: int,
    dry_run: bool,
) -> list[Cell]:
    return [
        now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        season,
        week,
        status,
        ", ".join(stages_ok),
        json.dumps(stages_failed) if stages_failed else "",
        json.dumps(rows_written),
        ", ".join(sources),
        " | ".join(warnings)[:5000],
        round(duration_s, 1),
        cfbd_calls,
        dry_run,
        git_short_sha(),
        "github-actions" if os.environ.get("GITHUB_ACTIONS") else "local",
    ]


def append_log(writer: SheetWriter, spec: TabSpec, row: list[Cell]) -> None:
    writer.append_rows(spec, [row])
