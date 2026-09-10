"""Game weather from venue coordinates + Open-Meteo hourly forecasts.

Per game: temperature at the kickoff hour, max precipitation probability and
total precipitation over the game window (kickoff to kickoff + 3h), mean wind
and max gust over the window, and a text condition from the WMO weather code.
Domes get condition "Dome" and blank outdoor fields. Games beyond the 16-day
forecast horizon get "beyond forecast".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from cfb_dfs.models import Game

GAME_HOURS = 3

WMO = {
    0: "Clear",
    1: "Mostly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Fog",
    51: "Drizzle",
    53: "Drizzle",
    55: "Drizzle",
    56: "Freezing drizzle",
    57: "Freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Showers",
    81: "Showers",
    82: "Heavy showers",
    85: "Snow showers",
    86: "Snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm w/ hail",
    99: "Thunderstorm w/ hail",
}


@dataclass(frozen=True)
class Venue:
    id: int
    name: str | None
    city: str | None
    state: str | None
    latitude: float | None
    longitude: float | None
    dome: bool


@dataclass
class GameWeather:
    game_id: int
    venue: str | None
    dome: bool
    city: str | None = None
    state: str | None = None
    temp_f: float | None = None
    precip_prob: int | None = None
    precip_in: float | None = None
    wind_mph: float | None = None
    gust_mph: float | None = None
    condition: str | None = None
    note: str | None = None


def venues_from_rows(rows: list[dict[str, Any]]) -> dict[int, Venue]:
    out: dict[int, Venue] = {}
    for v in rows:
        try:
            vid = int(v["id"])
        except (KeyError, TypeError, ValueError):
            continue
        out[vid] = Venue(
            id=vid,
            name=v.get("name"),
            city=v.get("city"),
            state=v.get("state"),
            latitude=_f(v.get("latitude")),
            longitude=_f(v.get("longitude")),
            dome=bool(v.get("dome")),
        )
    return out


def _f(x: Any) -> float | None:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _hour_key(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:00")


def summarize(game: Game, venue: Venue | None, forecast: dict[str, Any] | None) -> GameWeather:
    gw = GameWeather(
        game.id, venue.name if venue else game_venue_fallback(game), bool(venue and venue.dome)
    )
    if venue is not None:
        gw.city, gw.state = venue.city, venue.state
    if gw.dome:
        gw.condition = "Dome"
        return gw
    if venue is None or venue.latitude is None or venue.longitude is None:
        gw.note = "no venue coordinates"
        return gw
    if forecast is None:
        gw.note = "forecast unavailable"
        return gw
    hourly = forecast.get("hourly") or {}
    times: list[str] = hourly.get("time") or []
    idx = {t: i for i, t in enumerate(times)}
    kick = game.start_date.replace(minute=0, second=0, microsecond=0)
    hours = [kick + timedelta(hours=h) for h in range(GAME_HOURS + 1)]
    ids = [idx[_hour_key(h)] for h in hours if _hour_key(h) in idx]
    if not ids:
        gw.note = "beyond forecast"
        return gw

    def series(name: str) -> list[float]:
        vals = hourly.get(name) or []
        return [vals[i] for i in ids if i < len(vals) and vals[i] is not None]

    temp = series("temperature_2m")
    prob = series("precipitation_probability")
    amt = series("precipitation")
    wind = series("wind_speed_10m")
    gust = series("wind_gusts_10m")
    code = series("weather_code")
    gw.temp_f = round(temp[0], 0) if temp else None
    gw.precip_prob = int(max(prob)) if prob else None
    gw.precip_in = round(sum(amt), 2) if amt else None
    gw.wind_mph = round(sum(wind) / len(wind), 0) if wind else None
    gw.gust_mph = round(max(gust), 0) if gust else None
    gw.condition = WMO.get(int(code[0]), f"code {int(code[0])}") if code else None
    if len(ids) < len(hours):
        gw.note = "partial forecast window"
    return gw


def game_venue_fallback(game: Game) -> str | None:
    return getattr(game, "venue", None)
