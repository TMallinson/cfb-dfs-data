"""Configuration: YAML for settings, environment variables for secrets."""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

SeasonType = Literal["regular", "postseason"]


class CacheTTL(BaseModel):
    lines: float = 1
    games: float = 6
    calendar: float = 168
    teams: float = 168
    ppa: float = 6
    parquet: float = 6


class CacheConfig(BaseModel):
    dir: Path = Path("data/cache")
    ttl_hours: CacheTTL = CacheTTL()


class CfbdConfig(BaseModel):
    enabled: bool = True
    base_url: str = "https://api.collegefootballdata.com"
    line_provider_preference: list[str] = ["DraftKings", "Bovada", "ESPN Bet", "consensus"]


class EspnConfig(BaseModel):
    enabled: bool = True


class SportsdataverseConfig(BaseModel):
    enabled: bool = True
    release_base: str = "https://github.com/sportsdataverse/sportsdataverse-data/releases/download"
    seasons_for_model: list[int] = [2025, 2026]


class DraftKingsConfig(BaseModel):
    enabled: bool = True


class PffCsvConfig(BaseModel):
    enabled: bool = True
    dir: Path = Path("data/manual/pff")


class SourcesConfig(BaseModel):
    cfbd: CfbdConfig = CfbdConfig()
    espn: EspnConfig = EspnConfig()
    sportsdataverse: SportsdataverseConfig = SportsdataverseConfig()
    draftkings: DraftKingsConfig = DraftKingsConfig()
    pff_csv: PffCsvConfig = PffCsvConfig()


class KickoffWindow(BaseModel):
    name: str
    weekday: str
    start: str
    end: str


class SlatesConfig(BaseModel):
    kickoff_windows_ct: list[KickoffWindow] = []


class GarbageTime(BaseModel):
    q1: int | None = None
    q2: int | None = 28
    q3: int | None = 21
    q4: int | None = 14


class MetricsConfig(BaseModel):
    garbage_time: GarbageTime = GarbageTime()
    two_minute_filter: bool = True
    include_scrambles_in_dropback: bool = True
    recent_games_window: int = 3
    rank_population: str = "fbs"


class TabNames(BaseModel):
    main: str = "Main"
    pff: str = "PFF Ratings"
    vegas_raw: str = "Vegas Raw"
    pace_raw: str = "Pace Raw"
    epa_raw: str = "EPA Raw"
    rroe_raw: str = "RROE Raw"
    teams: str = "Teams"
    targets_raw: str = "Targets Raw"
    passing_raw: str = "Passing Raw"
    rushing_raw: str = "Rushing Raw"
    targets: str = "Targets"
    passing: str = "Passing"
    rushing: str = "Rushing"
    config: str = "Config"
    log: str = "Log"


class SheetConfig(BaseModel):
    tabs: TabNames = TabNames()
    protected_editor_emails: list[str] = []


class SeasonWindow(BaseModel):
    start: date
    end: date


class Settings(BaseModel):
    season: int
    week: int | None = None
    season_type: SeasonType = "regular"
    timezone: str = "America/Chicago"
    season_window: SeasonWindow | None = None
    cache: CacheConfig = CacheConfig()
    sources: SourcesConfig = SourcesConfig()
    slates: SlatesConfig = SlatesConfig()
    metrics: MetricsConfig = MetricsConfig()
    sheet: SheetConfig = SheetConfig()

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def now(self) -> datetime:
        return datetime.now(self.tz)


class Secrets(BaseModel):
    """Read from the environment only. Never logged, never serialized."""

    cfbd_api_key: str | None = Field(default=None, repr=False)
    sheet_id: str | None = Field(default=None, repr=False)
    google_credentials_path: str | None = Field(default=None, repr=False)
    google_service_account_b64: str | None = Field(default=None, repr=False)

    @classmethod
    def from_env(cls, dotenv_path: Path | None = None) -> Secrets:
        # .env is a local convenience; in CI the variables are injected as secrets.
        load_dotenv(dotenv_path or Path(".env"), override=False)
        return cls(
            cfbd_api_key=_clean(os.environ.get("CFBD_API_KEY")),
            sheet_id=_clean(os.environ.get("SHEET_ID")),
            google_credentials_path=_clean(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")),
            google_service_account_b64=_clean(os.environ.get("GOOGLE_SERVICE_ACCOUNT_B64")),
        )


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().strip('"').strip("'")
    return value or None


def load_settings(path: Path | str = "config.yaml") -> Settings:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return Settings.model_validate(raw)
