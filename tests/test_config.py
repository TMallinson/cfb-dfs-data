from __future__ import annotations

from pathlib import Path

from cfb_dfs.config import Secrets, load_settings

REPO = Path(__file__).resolve().parents[1]


def test_repo_config_loads():
    s = load_settings(REPO / "config.yaml")
    assert s.season == 2026
    assert s.timezone == "America/Chicago"
    assert s.metrics.garbage_time.q4 == 14
    assert s.sheet.tabs.main == "Main"


def test_secrets_never_in_repr(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CFBD_API_KEY", "  'abc123'  ")
    monkeypatch.setenv("SHEET_ID", "sheet")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_B64", raising=False)
    s = Secrets.from_env(dotenv_path=tmp_path / "nonexistent.env")
    assert s.cfbd_api_key == "abc123"
    assert "abc123" not in repr(s)
    assert "abc123" not in str(s)
