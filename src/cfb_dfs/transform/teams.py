"""Team index: canonical ids, display abbreviations, and DraftKings name matching."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from cfb_dfs.logging_setup import get_logger
from cfb_dfs.models import Team
from cfb_dfs.models.slates import DkTeam

log = get_logger(__name__)

_PUNCT = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")
_WORD_MAP = {
    "st": "state",
    "saint": "st",
    "univ": "university",
    "so": "southern",
    "no": "northern",
    "n": "north",
    "s": "south",
    "e": "east",
    "w": "west",
    "and": "",
}


def normalize(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.lower().replace("&", " and ").replace("'", "")
    s = _PUNCT.sub(" ", s)
    words = [_WORD_MAP.get(w, w) for w in s.split()]
    return _WS.sub(" ", " ".join(w for w in words if w)).strip()


@dataclass
class Overrides:
    dk_names: dict[str, str] = field(default_factory=dict)
    dk_abbreviations: dict[str, str] = field(default_factory=dict)
    display_abbreviations: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | str | None) -> Overrides:
        if not path or not Path(path).is_file():
            return cls()
        with open(path, encoding="utf-8") as fh:
            raw: dict[str, Any] = yaml.safe_load(fh) or {}
        return cls(
            dk_names={normalize(k): v for k, v in (raw.get("dk_names") or {}).items()},
            dk_abbreviations={
                str(k).upper(): v for k, v in (raw.get("dk_abbreviations") or {}).items()
            },
            display_abbreviations=dict(raw.get("display_abbreviations") or {}),
        )


@dataclass
class TeamIndex:
    teams: list[Team]
    overrides: Overrides = field(default_factory=Overrides)
    by_id: dict[int, Team] = field(init=False)
    by_school: dict[str, Team] = field(init=False)
    by_norm: dict[str, Team] = field(init=False)
    by_abbr: dict[str, Team] = field(init=False)
    display_abbr: dict[int, str] = field(init=False, default_factory=dict)
    unmatched: list[str] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self.by_id = {t.id: t for t in self.teams}
        self.by_school = {t.school: t for t in self.teams}
        self.by_norm = {}
        self.by_abbr = {}
        for t in self.teams:
            keys = {normalize(t.school)}
            if t.mascot:
                keys.add(normalize(f"{t.school} {t.mascot}"))
            for alt in t.alternate_names:
                keys.add(normalize(alt))
            for k in keys:
                self.by_norm.setdefault(k, t)
            if t.abbreviation:
                self.by_abbr.setdefault(t.abbreviation.upper(), t)
            self.display_abbr[t.id] = self.overrides.display_abbreviations.get(
                t.school, t.abbreviation or t.school.upper()[:6]
            )

    # -- lookups ---------------------------------------------------------------------------

    def get(self, team_id: int) -> Team | None:
        return self.by_id.get(team_id)

    def abbr(self, team_id: int) -> str:
        return self.display_abbr.get(team_id, str(team_id))

    def is_fbs(self, team_id: int) -> bool:
        t = self.by_id.get(team_id)
        return bool(t and (t.classification or "").lower() == "fbs")

    def fbs_ids(self) -> set[int]:
        return {t.id for t in self.teams if (t.classification or "").lower() == "fbs"}

    def match_dk(self, dk: DkTeam) -> Team | None:
        """Resolve a DraftKings team to a CFBD team. Order: overrides, full name,
        city-as-school, abbreviation. Records misses in `self.unmatched`."""
        full = normalize(f"{dk.city} {dk.nickname or ''}")
        city = normalize(dk.city)
        abbr = dk.abbreviation.upper()
        candidates = [
            self.by_school.get(self.overrides.dk_abbreviations.get(abbr, "")),
            self.by_school.get(self.overrides.dk_names.get(full, "")),
            self.by_norm.get(full),
            self.by_norm.get(city),
            self.by_abbr.get(abbr),
        ]
        for c in candidates:
            if c is not None:
                return c
        miss = f"{dk.city} {dk.nickname or ''} ({abbr})".strip()
        if miss not in self.unmatched:
            self.unmatched.append(miss)
            log.warning("teams.unmatched_dk_team", dk=miss)
        return None

    def adopt_dk_abbreviations(self, pairs: list[tuple[int, str]]) -> None:
        """Prefer DraftKings' abbreviation for teams DK lists this week (matches the
        sheet's historical key style), unless an override pins a display value."""
        for team_id, abbr in pairs:
            t = self.by_id.get(team_id)
            if t and t.school not in self.overrides.display_abbreviations and abbr:
                self.display_abbr[team_id] = abbr.upper()
