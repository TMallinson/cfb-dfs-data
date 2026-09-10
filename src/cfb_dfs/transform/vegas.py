"""Vegas lines -> per-team rows with implied totals.

Sign convention: source `spread` is home-perspective (negative = home favored).
Each team row stores `spread` from THAT team's perspective (negative = favored):
    home_spread = spread,  away_spread = -spread
    implied_total = total/2 - team_spread/2
which equals favorite = total/2 + |spread|/2 and underdog = total/2 - |spread|/2.
"""

from __future__ import annotations

from dataclasses import dataclass

from cfb_dfs.models import BookLine, GameLines


@dataclass(frozen=True)
class TeamLine:
    game_id: int
    team_id: int
    opponent_id: int
    is_home: bool
    book: str | None
    total: float | None
    spread: float | None  # this team's perspective, negative = favored
    implied_total: float | None
    spread_open: float | None
    total_open: float | None
    moneyline: float | None
    books_available: str


def choose_book(lines: list[BookLine], preference: list[str]) -> BookLine | None:
    """First provider in `preference` that has both a spread and a total; then any
    provider with both; then any with a total."""
    usable = [ln for ln in lines if ln.over_under is not None and ln.spread is not None]
    by_provider = {ln.provider.lower(): ln for ln in usable}
    for p in preference:
        if p.lower() in by_provider:
            return by_provider[p.lower()]
    if usable:
        return usable[0]
    with_total = [ln for ln in lines if ln.over_under is not None]
    return with_total[0] if with_total else None


def implied_total(total: float | None, team_spread: float | None) -> float | None:
    if total is None or team_spread is None:
        return None
    return round(total / 2 - team_spread / 2, 2)


def team_lines(game: GameLines, preference: list[str]) -> tuple[TeamLine, TeamLine]:
    book = choose_book(game.lines, preference)
    avail = ", ".join(sorted({ln.provider for ln in game.lines})) or "none"
    total = book.over_under if book else None
    home_spread = book.spread if book else None
    away_spread = -home_spread if home_spread is not None else None
    home_open = book.spread_open if book else None
    away_open = -home_open if home_open is not None else None
    total_open = book.over_under_open if book else None

    home = TeamLine(
        game_id=game.id,
        team_id=game.home_team_id,
        opponent_id=game.away_team_id,
        is_home=True,
        book=book.provider if book else None,
        total=total,
        spread=home_spread,
        implied_total=implied_total(total, home_spread),
        spread_open=home_open,
        total_open=total_open,
        moneyline=book.home_moneyline if book else None,
        books_available=avail,
    )
    away = TeamLine(
        game_id=game.id,
        team_id=game.away_team_id,
        opponent_id=game.home_team_id,
        is_home=False,
        book=book.provider if book else None,
        total=total,
        spread=away_spread,
        implied_total=implied_total(total, away_spread),
        spread_open=away_open,
        total_open=total_open,
        moneyline=book.away_moneyline if book else None,
        books_available=avail,
    )
    return home, away


def all_team_lines(games: list[GameLines], preference: list[str]) -> dict[int, list[TeamLine]]:
    """game_id -> [home, away]."""
    return {g.id: list(team_lines(g, preference)) for g in games}
