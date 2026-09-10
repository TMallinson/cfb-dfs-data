"""Open-Meteo hourly forecasts (free, no key, 16-day horizon).

One request covers every venue of the week (comma-separated latitude/longitude
lists), so a run costs a single call. https://open-meteo.com/en/docs
"""

from __future__ import annotations

import hashlib
from typing import Any

import httpx

from cfb_dfs.logging_setup import get_logger
from cfb_dfs.sources.cache import DiskCache, safe_key
from cfb_dfs.sources.http import SourceError, get_with_retry, make_client

log = get_logger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HOURLY = [
    "temperature_2m",
    "precipitation_probability",
    "precipitation",
    "wind_speed_10m",
    "wind_gusts_10m",
    "weather_code",
]
TTL_HOURS = 3.0


def fetch_forecasts(
    cache: DiskCache, season: int, week: int, points: list[tuple[float, float]]
) -> list[dict[str, Any]]:
    """Return one forecast object per (lat, lon) point, in order."""
    if not points:
        return []
    digest = hashlib.sha1(";".join(f"{la:.3f},{lo:.3f}" for la, lo in points).encode()).hexdigest()[
        :12
    ]
    key = safe_key(f"forecast__week={week}__{len(points)}pts__{digest}")
    hit = cache.get_json(season, "openmeteo", key, TTL_HOURS)
    if hit is not None:
        return list(hit)
    params = {
        "latitude": ",".join(f"{la:.4f}" for la, _ in points),
        "longitude": ",".join(f"{lo:.4f}" for _, lo in points),
        "hourly": ",".join(HOURLY),
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": "UTC",
        "forecast_days": 16,
    }
    client = make_client()
    try:
        resp = get_with_retry(client, FORECAST_URL, params)
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SourceError(f"Open-Meteo request failed: {exc.__class__.__name__}") from exc
    finally:
        client.close()
    data = payload if isinstance(payload, list) else [payload]
    if len(data) != len(points):
        raise SourceError(f"Open-Meteo returned {len(data)} forecasts for {len(points)} points")
    cache.put_json(season, "openmeteo", key, data)
    log.info("openmeteo.fetched", points=len(points))
    return data
