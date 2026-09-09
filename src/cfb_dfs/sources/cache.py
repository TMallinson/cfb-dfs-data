"""Disk cache for raw source responses.

Layout: <dir>/<season>/<namespace>/<key>.<ext>. JSON for API payloads, raw bytes
for parquet. Entries carry a TTL; `refresh=True` bypasses reads but still writes.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cfb_dfs.logging_setup import get_logger

log = get_logger(__name__)

_SAFE = re.compile(r"[^A-Za-z0-9_.=-]+")


def safe_key(*parts: Any) -> str:
    return "__".join(_SAFE.sub("-", str(p)) for p in parts if p is not None and p != "")


@dataclass
class DiskCache:
    root: Path
    refresh: bool = False

    def path(self, season: int | str, namespace: str, key: str, ext: str = "json") -> Path:
        return self.root / str(season) / namespace / f"{key}.{ext}"

    def _fresh(self, p: Path, ttl_hours: float | None) -> bool:
        if self.refresh or not p.exists():
            return False
        if ttl_hours is None:
            return True
        return (time.time() - p.stat().st_mtime) < ttl_hours * 3600

    def get_json(
        self, season: int | str, namespace: str, key: str, ttl_hours: float | None
    ) -> Any | None:
        p = self.path(season, namespace, key)
        if not self._fresh(p, ttl_hours):
            return None
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)

    def put_json(self, season: int | str, namespace: str, key: str, payload: Any) -> Path:
        p = self.path(season, namespace, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        tmp.replace(p)
        return p

    def get_bytes_path(
        self, season: int | str, namespace: str, key: str, ext: str, ttl_hours: float | None
    ) -> Path | None:
        p = self.path(season, namespace, key, ext)
        return p if self._fresh(p, ttl_hours) else None

    def put_bytes(
        self, season: int | str, namespace: str, key: str, ext: str, payload: bytes
    ) -> Path:
        p = self.path(season, namespace, key, ext)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(payload)
        tmp.replace(p)
        return p
