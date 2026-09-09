from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CalendarWeek(BaseModel):
    """One row of CFBD /calendar."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    season: int
    week: int
    season_type: str = Field(alias="seasonType")
    start_date: datetime = Field(alias="startDate")
    end_date: datetime = Field(alias="endDate")
    first_game_start: datetime | None = Field(default=None, alias="firstGameStart")
    last_game_start: datetime | None = Field(default=None, alias="lastGameStart")


class Game(BaseModel):
    """One row of CFBD /games. `id` equals the ESPN event id."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int
    season: int
    week: int
    season_type: str = Field(alias="seasonType")
    start_date: datetime = Field(alias="startDate")
    start_time_tbd: bool = Field(default=False, alias="startTimeTBD")
    completed: bool = False
    neutral_site: bool = Field(default=False, alias="neutralSite")
    home_id: int = Field(alias="homeId")
    home_team: str = Field(alias="homeTeam")
    home_conference: str | None = Field(default=None, alias="homeConference")
    home_classification: str | None = Field(default=None, alias="homeClassification")
    home_points: int | None = Field(default=None, alias="homePoints")
    away_id: int = Field(alias="awayId")
    away_team: str = Field(alias="awayTeam")
    away_conference: str | None = Field(default=None, alias="awayConference")
    away_classification: str | None = Field(default=None, alias="awayClassification")
    away_points: int | None = Field(default=None, alias="awayPoints")
