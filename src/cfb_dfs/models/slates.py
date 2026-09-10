from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class DkTeam(BaseModel):
    dk_team_id: int | None = None
    city: str  # e.g. "Penn State"
    nickname: str | None = None  # e.g. "Nittany Lions"
    abbreviation: str  # e.g. "PSU"


class DkGame(BaseModel):
    competition_id: int | None = None
    name: str  # "PSU @ TEMP"
    start_time: datetime  # tz-aware UTC
    home: DkTeam
    away: DkTeam


class Slate(BaseModel):
    """One DraftKings draft group (or a manual slate from the Config tab)."""

    slate_id: str  # DK draft group id, or "manual:<name>"
    label: str  # "Main", "Afternoon", "Night", "Thu", "SHW OU@MICH"
    game_type: str  # Classic | Showdown | Snake | Manual
    start_time: datetime  # tz-aware
    games: list[DkGame]
    source: str  # "draftkings" | "config" | "kickoff-window"

    @property
    def is_classic(self) -> bool:
        return self.game_type == "Classic"
