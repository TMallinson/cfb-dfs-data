from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class BookLine(BaseModel):
    """One sportsbook's line for a game.

    Sign convention (CFBD and ESPN agree): `spread` is from the HOME team's
    perspective, negative = home favored. "Missouri -5.5" with Missouri away
    is stored as spread=+5.5.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    provider: str
    spread: float | None = None
    formatted_spread: str | None = Field(default=None, alias="formattedSpread")
    spread_open: float | None = Field(default=None, alias="spreadOpen")
    over_under: float | None = Field(default=None, alias="overUnder")
    over_under_open: float | None = Field(default=None, alias="overUnderOpen")
    home_moneyline: float | None = Field(default=None, alias="homeMoneyline")
    away_moneyline: float | None = Field(default=None, alias="awayMoneyline")


class GameLines(BaseModel):
    """One row of CFBD /lines."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int
    season: int
    week: int
    season_type: str = Field(alias="seasonType")
    start_date: datetime = Field(alias="startDate")
    home_team_id: int = Field(alias="homeTeamId")
    home_team: str = Field(alias="homeTeam")
    home_classification: str | None = Field(default=None, alias="homeClassification")
    away_team_id: int = Field(alias="awayTeamId")
    away_team: str = Field(alias="awayTeam")
    away_classification: str | None = Field(default=None, alias="awayClassification")
    lines: list[BookLine] = Field(default_factory=list)
