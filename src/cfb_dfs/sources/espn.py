"""ESPN public scoreboard: schedule + DraftKings odds fallback (no auth).

GET https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard
    ?dates={season}&seasontype=2&week={week}&groups=80&limit=400

Event ids and team ids are the same ids CFBD uses, so the output is shaped
exactly like CFBD /games and /lines records.
"""

from __future__ import annotations

from typing import Any

import httpx

from cfb_dfs.config import CacheTTL
from cfb_dfs.logging_setup import get_logger
from cfb_dfs.models import BookLine, Game, GameLines
from cfb_dfs.sources.cache import DiskCache, safe_key
from cfb_dfs.sources.http import SourceError, get_with_retry, make_client

log = get_logger(__name__)

SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"
)
FBS_GROUP = 80


def fetch_scoreboard(
    cache: DiskCache, ttl: CacheTTL, season: int, week: int, season_type: int = 2
) -> dict[str, Any]:
    key = safe_key(f"scoreboard__week={week}__st={season_type}")
    hit = cache.get_json(season, "espn", key, ttl.lines)
    if hit is not None:
        return hit
    client = make_client()
    try:
        resp = get_with_retry(
            client,
            SCOREBOARD_URL,
            {
                "dates": season,
                "seasontype": season_type,
                "week": week,
                "groups": FBS_GROUP,
                "limit": 400,
            },
        )
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SourceError(f"ESPN scoreboard failed: {exc.__class__.__name__}") from exc
    finally:
        client.close()
    cache.put_json(season, "espn", key, payload)
    return payload


def scoreboard_to_games_and_lines(payload: dict[str, Any]) -> tuple[list[Game], list[GameLines]]:
    games: list[Game] = []
    lines: list[GameLines] = []
    season = int(payload.get("season", {}).get("year", 0))
    week = int(payload.get("week", {}).get("number", 0))
    for ev in payload.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        home = next((c for c in comp.get("competitors", []) if c.get("homeAway") == "home"), None)
        away = next((c for c in comp.get("competitors", []) if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue
        status = ev.get("status", {}).get("type", {})
        base = {
            "id": int(ev["id"]),
            "season": season,
            "week": week,
            "seasonType": "regular",
            "startDate": ev["date"],
        }
        games.append(
            Game.model_validate(
                {
                    **base,
                    "completed": bool(status.get("completed")),
                    "neutralSite": bool(comp.get("neutralSite")),
                    "homeId": int(home["team"]["id"]),
                    "homeTeam": home["team"].get("location") or home["team"].get("displayName"),
                    "homeConference": (home.get("team", {}).get("conferenceId")),
                    "awayId": int(away["team"]["id"]),
                    "awayTeam": away["team"].get("location") or away["team"].get("displayName"),
                    "homePoints": _int_or_none(home.get("score")),
                    "awayPoints": _int_or_none(away.get("score")),
                }
            )
        )
        books: list[BookLine] = []
        for o in comp.get("odds", []):
            provider = (o.get("provider") or {}).get("name") or "unknown"
            books.append(
                BookLine(
                    provider=provider,
                    spread=o.get("spread"),
                    overUnder=o.get("overUnder"),
                    formattedSpread=o.get("details"),
                )
            )
        lines.append(
            GameLines.model_validate(
                {
                    **base,
                    "homeTeamId": int(home["team"]["id"]),
                    "homeTeam": home["team"].get("location") or "",
                    "awayTeamId": int(away["team"]["id"]),
                    "awayTeam": away["team"].get("location") or "",
                    "lines": [b.model_dump(by_alias=True) for b in books],
                }
            )
        )
    return games, lines


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
