from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture(scope="session")
def load_json():
    def _load(name: str):
        with open(FIXTURES / name, encoding="utf-8") as fh:
            return json.load(fh)

    return _load
