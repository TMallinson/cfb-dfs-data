"""Pipeline orchestration. Stages are added phase by phase.

Contract: each stage fetches, transforms, and returns value blocks for its
tabs; a stage failure is recorded and the other stages still run; the sheet
write happens once at the end; exit code is non-zero if any stage failed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from cfb_dfs.config import Secrets, Settings
from cfb_dfs.logging_setup import get_logger

log = get_logger(__name__)

ALL_STAGES = ["slates", "vegas", "pace", "epa", "rroe", "players"]


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


def run_pipeline(
    settings: Settings, secrets: Secrets, stages: list[str] | None, dry_run: bool, refresh: bool
) -> int:
    from cfb_dfs.cli import _cfbd, detect_week

    started = time.monotonic()
    wanted = stages or ALL_STAGES
    unknown = sorted(set(wanted) - set(ALL_STAGES))
    if unknown:
        log.error("pipeline.unknown_stages", unknown=unknown, allowed=ALL_STAGES)
        return 2

    cfbd = _cfbd(settings, secrets, refresh=refresh) if settings.sources.cfbd.enabled else None
    week, week_source = detect_week(settings, cfbd)
    result = RunResult(week=week)
    log.info(
        "pipeline.start",
        season=settings.season,
        week=week,
        week_source=week_source,
        stages=wanted,
        dry_run=dry_run,
        refresh=refresh,
    )

    for stage in wanted:
        result.stages_failed[stage] = "not implemented yet (Phase 3+)"

    result.duration_s = time.monotonic() - started
    log.info(
        "pipeline.done",
        ok=result.stages_ok,
        failed=result.stages_failed,
        duration_s=round(result.duration_s, 1),
        cfbd_calls=cfbd.calls_made if cfbd else 0,
    )
    return result.exit_code
