"""Assign the week's games to DFS slates.

Priority: DraftKings slates -> manual Config-tab slates -> kickoff-window
heuristic. A game can be on several slates; `primary` is the first classic
slate by start time (else the first showdown) and drives grouping on the main tab.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo

from cfb_dfs.config import KickoffWindow
from cfb_dfs.logging_setup import get_logger
from cfb_dfs.models import Game
from cfb_dfs.models.slates import DkGame, DkTeam, Slate
from cfb_dfs.transform.teams import TeamIndex

log = get_logger(__name__)

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class GameSlates:
    game: Game
    labels: list[str] = field(default_factory=list)  # all slates containing the game
    primary: str | None = None
    primary_start: datetime | None = None
    source: str = "none"

    @property
    def label_text(self) -> str:
        return "; ".join(self.labels)


def _game_key(home_id: int, away_id: int) -> tuple[int, int]:
    return (home_id, away_id)


def match_dk_games(
    slates: list[Slate], games: list[Game], teams: TeamIndex
) -> tuple[dict[int, list[Slate]], list[str]]:
    """Map CFBD game id -> slates containing it. Returns (mapping, warnings)."""
    by_pair = {_game_key(g.home_id, g.away_id): g for g in games}
    by_pair_rev = {_game_key(g.away_id, g.home_id): g for g in games}  # neutral-site flips
    result: dict[int, list[Slate]] = {}
    warnings: list[str] = []
    for slate in slates:
        for dk_game in slate.games:
            home = teams.match_dk(dk_game.home)
            away = teams.match_dk(dk_game.away)
            if home is None or away is None:
                warnings.append(f"{slate.label}: could not map DK game {dk_game.name} to teams")
                continue
            key = _game_key(home.id, away.id)
            g = by_pair.get(key) or by_pair_rev.get(key)
            if g is None:
                warnings.append(f"{slate.label}: DK game {dk_game.name} not in CFBD week schedule")
                continue
            result.setdefault(g.id, []).append(slate)
    return result, warnings


def assign(
    games: list[Game],
    dk_slates: list[Slate],
    manual_slates: list[Slate],
    windows: list[KickoffWindow],
    teams: TeamIndex,
    tz: ZoneInfo,
    include_unslated: bool = False,
) -> tuple[list[GameSlates], list[str]]:
    warnings: list[str] = []
    slates = list(dk_slates)
    dk_labels = {s.label for s in dk_slates}
    for m in manual_slates:
        if m.label in dk_labels:
            slates = [s for s in slates if s.label != m.label]
        slates.append(m)

    mapping, w = match_dk_games(slates, games, teams)
    warnings.extend(w)

    out: list[GameSlates] = []
    for g in games:
        gs = GameSlates(game=g)
        found = sorted(mapping.get(g.id, []), key=lambda s: (not s.is_classic, s.start_time))
        if found:
            gs.labels = [s.label for s in found]
            gs.primary = found[0].label
            gs.primary_start = found[0].start_time
            gs.source = found[0].source
        elif not slates:
            label = kickoff_window_label(g.start_date, windows, tz)
            gs.labels = [label]
            gs.primary = label
            gs.primary_start = g.start_date
            gs.source = "kickoff-window"
        if gs.labels or include_unslated:
            out.append(gs)

    if not slates:
        warnings.append("No DraftKings or Config slates; used kickoff-window heuristic")
    out.sort(key=lambda x: (x.primary_start or x.game.start_date, x.game.start_date, x.game.id))
    return out, warnings


def kickoff_window_label(start_utc: datetime, windows: list[KickoffWindow], tz: ZoneInfo) -> str:
    local = start_utc.astimezone(tz)
    day = WEEKDAYS[local.weekday()]
    for w in windows:
        if w.weekday != day:
            continue
        t0 = time.fromisoformat(w.start)
        t1 = time.fromisoformat(w.end)
        if t0 <= local.time() <= t1:
            return w.name
    return day


def manual_slates_from_rows(
    rows: list[list[str]], games: list[Game], teams: TeamIndex, tz: ZoneInfo
) -> list[Slate]:
    """Config tab rows: [slate_name, team_abbrevs_csv, weekday, start_hhmm, end_hhmm].

    Either a team list or a weekday+window selects games. Blank rows ignored.
    """
    out: list[Slate] = []
    abbr_to_id = {teams.abbr(t.id).upper(): t.id for t in teams.teams}
    for row in rows:
        cells = [str(c).strip() for c in row] + [""] * 5
        name, team_csv, weekday, start, end = cells[:5]
        if not name or name.lower().startswith("slate"):
            continue
        selected: list[Game] = []
        if team_csv:
            wanted = {a.strip().upper() for a in team_csv.split(",") if a.strip()}
            ids = {abbr_to_id[a] for a in wanted if a in abbr_to_id}
            selected = [g for g in games if g.home_id in ids or g.away_id in ids]
        elif weekday and start and end:
            win = [KickoffWindow(name=name, weekday=weekday[:3].title(), start=start, end=end)]
            selected = [g for g in games if kickoff_window_label(g.start_date, win, tz) == name]
        if not selected:
            log.warning("slates.manual_empty", slate=name)
            continue
        dk_games = [
            DkGame(
                name=f"{teams.abbr(g.away_id)} @ {teams.abbr(g.home_id)}",
                start_time=g.start_date,
                home=DkTeam(city=_school(teams, g.home_id), abbreviation=teams.abbr(g.home_id)),
                away=DkTeam(city=_school(teams, g.away_id), abbreviation=teams.abbr(g.away_id)),
            )
            for g in selected
        ]
        out.append(
            Slate(
                slate_id=f"manual:{name}",
                label=name,
                game_type="Classic",
                start_time=min(g.start_date for g in selected),
                games=dk_games,
                source="config",
            )
        )
    return out


def _school(teams: TeamIndex, team_id: int) -> str:
    t = teams.get(team_id)
    return t.school if t else str(team_id)
