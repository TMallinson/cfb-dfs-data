"""DraftKings public lobby + draftables endpoints (unauthenticated, read-only).

Lobby:      GET https://www.draftkings.com/lobby/getcontests?sport=CFB
Draftables: GET https://api.draftkings.com/draftgroups/v1/draftgroups/{id}/draftables?format=json

No login, no cookies, no bot-protection bypass. If either call fails the
caller falls back to Config-tab / kickoff-window slates.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from cfb_dfs.config import CacheTTL
from cfb_dfs.logging_setup import get_logger
from cfb_dfs.models.slates import DkGame, DkTeam, Slate
from cfb_dfs.sources.cache import DiskCache, safe_key
from cfb_dfs.sources.http import SourceError, get_with_retry, make_client

log = get_logger(__name__)

LOBBY_URL = "https://www.draftkings.com/lobby/getcontests"
DRAFTABLES_URL = "https://api.draftkings.com/draftgroups/v1/draftgroups/{id}/draftables"

GAME_TYPES = {94: "Classic", 95: "Showdown", 364: "Single Stat - Touchdowns", 377: "Snake"}
# Formats that mirror a classic slate's games and add no new games.
SKIP_GAME_TYPES = {"Snake"}
SKIP_GAME_TYPE_PREFIXES = ("Single Stat",)
EST = ZoneInfo("America/New_York")


def _parse_dk_time(value: str) -> datetime:
    """DK timestamps look like 2026-09-12T16:00:00.0000000Z (7 fractional digits)."""
    v = value.rstrip("Z")
    if "." in v:
        head, frac = v.split(".", 1)
        v = f"{head}.{frac[:6]}"
    dt = datetime.fromisoformat(v)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def slate_label(game_type: str, suffix: str | None, start_utc: datetime) -> str:
    """Turn DK's ContestStartTimeSuffix into a short label.

    "" on Saturday -> Main; " (Afternoon)" -> Afternoon; weekday classic with no
    suffix -> weekday name; showdown " (OU @ MICH)" -> "SHW OU@MICH".
    """
    text = (suffix or "").strip().strip("()").strip()
    local = start_utc.astimezone(EST)
    if game_type == "Showdown":
        return f"SHW {text.replace(' ', '')}" if text else f"SHW {local:%a %I:%M%p}"
    if text:
        return text
    return "Main" if local.weekday() == 5 else local.strftime("%a")


def fetch_slates(cache: DiskCache, ttl: CacheTTL, season: int, week: int) -> list[Slate]:
    """Return every non-snake CFB slate currently in the DK lobby with its games."""
    client = make_client()
    try:
        lobby = _cached_json(
            client, cache, season, f"lobby__week={week}", ttl.lines, LOBBY_URL, {"sport": "CFB"}
        )
        groups = lobby.get("DraftGroups", []) if isinstance(lobby, dict) else []
        if not groups:
            raise SourceError("DraftKings lobby returned no CFB draft groups")
        slates: list[Slate] = []
        type_names = dict(GAME_TYPES)
        for gt in lobby.get("GameTypes", []) if isinstance(lobby, dict) else []:
            if gt.get("GameTypeId") is not None and gt.get("Name"):
                type_names[int(gt["GameTypeId"])] = _canonical_type(str(gt["Name"]))
        for g in groups:
            if g.get("Sport") != "CFB":
                continue
            game_type = type_names.get(int(g.get("GameTypeId", 0)), f"Type{g.get('GameTypeId')}")
            if game_type in SKIP_GAME_TYPES or game_type.startswith(SKIP_GAME_TYPE_PREFIXES):
                log.info(
                    "draftkings.skip_group", draft_group=g.get("DraftGroupId"), game_type=game_type
                )
                continue
            dg_id = int(g["DraftGroupId"])
            start = _parse_dk_time(g["StartDate"])
            payload = _cached_json(
                client,
                cache,
                season,
                f"draftables__dg={dg_id}",
                ttl.lines,
                DRAFTABLES_URL.format(id=dg_id),
                {"format": "json"},
            )
            games = [_parse_competition(c) for c in payload.get("competitions", [])]
            if not games:
                log.warning("draftkings.empty_group", draft_group=dg_id, game_type=game_type)
                continue
            slates.append(
                Slate(
                    slate_id=str(dg_id),
                    label=slate_label(game_type, g.get("ContestStartTimeSuffix"), start),
                    game_type=game_type,
                    start_time=start,
                    games=games,
                    source="draftkings",
                )
            )
        log.info(
            "draftkings.slates",
            count=len(slates),
            labels=[f"{s.label}({len(s.games)})" for s in slates],
        )
        return slates
    except httpx.HTTPError as exc:
        raise SourceError(f"DraftKings request failed: {exc.__class__.__name__}") from exc
    finally:
        client.close()


def _canonical_type(name: str) -> str:
    if name.startswith("Showdown"):
        return "Showdown"
    return name


def _parse_competition(c: dict[str, Any]) -> DkGame:
    def team(t: dict[str, Any]) -> DkTeam:
        return DkTeam(
            dk_team_id=t.get("teamId"),
            city=str(t.get("city") or "").strip(),
            nickname=(t.get("teamName") or None),
            abbreviation=str(t.get("abbreviation") or "").strip(),
        )

    return DkGame(
        competition_id=c.get("competitionId"),
        name=str(c.get("name") or ""),
        start_time=_parse_dk_time(c["startTime"]),
        home=team(c.get("homeTeam", {})),
        away=team(c.get("awayTeam", {})),
    )


def _cached_json(
    client: httpx.Client,
    cache: DiskCache,
    season: int,
    key: str,
    ttl_hours: float,
    url: str,
    params: dict[str, Any],
) -> Any:
    hit = cache.get_json(season, "draftkings", safe_key(key), ttl_hours)
    if hit is not None:
        return hit
    resp = get_with_retry(client, url, params)
    try:
        payload = resp.json()
    except ValueError as exc:
        raise SourceError(f"DraftKings returned non-JSON from {url}") from exc
    cache.put_json(season, "draftkings", safe_key(key), payload)
    return payload
