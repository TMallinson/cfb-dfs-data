from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Team(BaseModel):
    """A team as returned by CFBD /teams/fbs (ids are shared with ESPN)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int
    school: str
    mascot: str | None = None
    abbreviation: str | None = None
    alternate_names: list[str] = Field(default_factory=list, alias="alternateNames")
    conference: str | None = None
    division: str | None = None
    classification: str | None = None
    color: str | None = None
    logos: list[str] = Field(default_factory=list)

    @field_validator("alternate_names", "logos", mode="before")
    @classmethod
    def _none_to_list(cls, v: object) -> object:
        return [] if v is None else v
