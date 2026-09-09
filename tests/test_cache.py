from __future__ import annotations

import os
import time
from pathlib import Path

from cfb_dfs.sources.cache import DiskCache, safe_key


def test_safe_key_strips_unsafe_chars():
    assert safe_key("lines", "year=2026", "week=2") == "lines__year=2026__week=2"
    assert "/" not in safe_key("a/b", "c d")


def test_json_roundtrip_and_ttl(tmp_path: Path):
    cache = DiskCache(tmp_path)
    assert cache.get_json(2026, "cfbd", "k", 1) is None
    p = cache.put_json(2026, "cfbd", "k", {"a": 1})
    assert p.exists()
    assert cache.get_json(2026, "cfbd", "k", 1) == {"a": 1}
    # Expire it by back-dating the mtime two hours.
    old = time.time() - 7200
    os.utime(p, (old, old))
    assert cache.get_json(2026, "cfbd", "k", 1) is None
    assert cache.get_json(2026, "cfbd", "k", None) == {"a": 1}


def test_refresh_bypasses_reads_but_still_writes(tmp_path: Path):
    cache = DiskCache(tmp_path, refresh=True)
    cache.put_json(2026, "cfbd", "k", [1])
    assert cache.get_json(2026, "cfbd", "k", None) is None
    assert DiskCache(tmp_path).get_json(2026, "cfbd", "k", None) == [1]
