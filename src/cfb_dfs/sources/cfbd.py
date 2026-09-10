"""Thin typed client for the CollegeFootballData REST API (v2).

Host: https://api.collegefootballdata.com   Auth: `Authorization: Bearer <key>`.
Free tier is 1,000 calls/month, so every call goes through the disk cache and
the client counts calls per process so the run log can report usage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from cfb_dfs.config import CacheTTL, CfbdConfig
from cfb_dfs.logging_setup import get_logger
from cfb_dfs.models import CalendarWeek, Game, GameLines, Team
from cfb_dfs.sources.cache import DiskCache, safe_key
from cfb_dfs.sources.http import SourceError, get_with_retry, make_client

log = get_logger(__name__)


@dataclass
class CfbdClient:
    api_key: str
    cache: DiskCache
    ttl: CacheTTL
    config: CfbdConfig = field(default_factory=CfbdConfig)
    calls_made: int = 0
    _client: httpx.Client | None = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = make_client(
                self.config.base_url, {"Authorization": f"Bearer {self.api_key}"}
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # -- low level -------------------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            resp = get_with_retry(self.client, path, params)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 401:
                raise SourceError(
                    "CFBD rejected the API key (401). Regenerate at "
                    "https://collegefootballdata.com/key"
                ) from exc
            raise SourceError(f"CFBD {path} failed with HTTP {status}") from exc
        except httpx.HTTPError as exc:
            raise SourceError(f"CFBD {path} failed: {exc.__class__.__name__}") from exc
        self.calls_made += 1
        log.debug("cfbd.call", path=path, params=params, calls_made=self.calls_made)
        return resp.json()

    def _cached(
        self,
        season: int,
        namespace: str,
        ttl_hours: float | None,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        key = safe_key(path.strip("/"), *(f"{k}={v}" for k, v in sorted((params or {}).items())))
        hit = self.cache.get_json(season, namespace, key, ttl_hours)
        if hit is not None:
            log.debug("cfbd.cache_hit", namespace=namespace, key=key)
            return hit
        payload = self._get(path, params)
        self.cache.put_json(season, namespace, key, payload)
        return payload

    # -- endpoints -------------------------------------------------------------------------

    def info(self) -> dict[str, Any]:
        """Account tier and remaining calls. Never cached; costs one call."""
        data = self._get("/info")
        return data if isinstance(data, dict) else {}

    def calendar(self, year: int) -> list[CalendarWeek]:
        data = self._cached(year, "cfbd", self.ttl.calendar, "/calendar", {"year": year})
        return [CalendarWeek.model_validate(row) for row in data]

    def teams(self, year: int) -> list[Team]:
        """Every team in every division for the season (FBS + FCS opponents needed)."""
        data = self._cached(year, "cfbd", self.ttl.teams, "/teams", {"year": year})
        return [Team.model_validate(row) for row in data]

    def teams_fbs(self, year: int) -> list[Team]:
        data = self._cached(year, "cfbd", self.ttl.teams, "/teams/fbs", {"year": year})
        return [Team.model_validate(row) for row in data]

    def games(
        self,
        year: int,
        week: int | None = None,
        season_type: str = "regular",
        classification: str | None = None,
    ) -> list[Game]:
        params = {
            "year": year,
            "week": week,
            "seasonType": season_type,
            "classification": classification,
        }
        data = self._cached(year, "cfbd", self.ttl.games, "/games", params)
        return [Game.model_validate(row) for row in data]

    def lines(self, year: int, week: int, season_type: str = "regular") -> list[GameLines]:
        params = {"year": year, "week": week, "seasonType": season_type}
        data = self._cached(year, "cfbd", self.ttl.lines, "/lines", params)
        return [GameLines.model_validate(row) for row in data]

    def ppa_teams(self, year: int, exclude_garbage_time: bool = True) -> list[dict[str, Any]]:
        params = {"year": year, "excludeGarbageTime": str(exclude_garbage_time).lower()}
        data = self._cached(year, "cfbd", self.ttl.ppa, "/ppa/teams", params)
        return list(data)

    def ppa_games(
        self,
        year: int,
        week: int | None = None,
        exclude_garbage_time: bool = True,
        season_type: str = "regular",
    ) -> list[dict[str, Any]]:
        params = {
            "year": year,
            "week": week,
            "seasonType": season_type,
            "excludeGarbageTime": str(exclude_garbage_time).lower(),
        }
        data = self._cached(year, "cfbd", self.ttl.ppa, "/ppa/games", params)
        return list(data)
