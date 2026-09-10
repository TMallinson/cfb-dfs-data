"""sportsdataverse parquet releases (ESPN-derived play-by-play, drives, rosters, teams).

Assets live at
  https://github.com/sportsdataverse/sportsdataverse-data/releases/download/<tag>/<file>
and are rebuilt daily by github.com/sportsdataverse/cfbfastR-cfb-data. Downloads
are cached on disk (TTL `cache.ttl_hours.parquet`); `--refresh` re-downloads.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import polars as pl

from cfb_dfs.config import CacheTTL, SportsdataverseConfig
from cfb_dfs.logging_setup import get_logger
from cfb_dfs.sources.cache import DiskCache
from cfb_dfs.sources.http import SourceError, make_client

log = get_logger(__name__)

ASSETS: dict[str, tuple[str, str]] = {
    "pbp": ("espn_cfb_pbp", "play_by_play_{season}.parquet"),
    "drives": ("espn_cfb_drives", "drives_{season}.parquet"),
    "teams": ("espn_cfb_teams", "cfb_teams_{season}.parquet"),
    "rosters": ("espn_cfb_rosters", "rosters_{season}.parquet"),
    "game_rosters": ("espn_cfb_game_rosters", "game_rosters_{season}.parquet"),
    "player_box": ("espn_cfb_player_box", "player_box_{season}.parquet"),
    "adv_receiving": ("espn_cfb_adv_receiving", "adv_receiving_{season}.parquet"),
    "adv_passing": ("espn_cfb_adv_passing", "adv_passing_{season}.parquet"),
    "adv_rushing": ("espn_cfb_adv_rushing", "adv_rushing_{season}.parquet"),
}

# Only the columns the transforms use; keeps memory small for the ~60 MB season file.
PBP_COLUMNS = [
    "game_id",
    "season",
    "week",
    "game_play_number",
    "period",
    "half",
    "clock.minutes",
    "clock.seconds",
    "pos_team_id",
    "pos_team",
    "def_pos_team_id",
    "def_pos_team",
    "homeTeamId",
    "awayTeamId",
    "down",
    "distance",
    "start.yardsToEndzone",
    "pos_score_diff_start",
    "scrimmage_play",
    "pass",
    "rush",
    "pass_attempt",
    "sack",
    "completion",
    "kneel_down",
    "penalty_no_play",
    "type.text",
    "text",
    "EPA",
    "passer_player_name",
    "passer_player_id",
    "rusher_player_name",
    "rusher_player_id",
    "receiver_player_name",
    "receiver_player_id",
    "drive.id",
    "start.posTeamTimeouts",
    "statYardage",
]


def asset_url(cfg: SportsdataverseConfig, asset: str, season: int) -> str:
    tag, pattern = ASSETS[asset]
    return f"{cfg.release_base}/{tag}/{pattern.format(season=season)}"


def fetch_parquet(
    cache: DiskCache, ttl: CacheTTL, cfg: SportsdataverseConfig, season: int, asset: str
) -> Path:
    """Return a local path to the asset, downloading if the cache is stale."""
    key = ASSETS[asset][1].format(season=season).removesuffix(".parquet")
    hit = cache.get_bytes_path(season, "sportsdataverse", key, "parquet", ttl.parquet)
    if hit is not None:
        return hit
    url = asset_url(cfg, asset, season)
    client = make_client()
    try:
        with client.stream("GET", url) as resp:
            if resp.status_code != 200:
                raise SourceError(
                    f"sportsdataverse {asset} {season}: HTTP {resp.status_code} {url}"
                )
            payload = b"".join(resp.iter_bytes())
    except httpx.HTTPError as exc:
        raise SourceError(f"sportsdataverse {asset} {season} download failed: {exc}") from exc
    finally:
        client.close()
    if len(payload) < 1000:
        raise SourceError(f"sportsdataverse {asset} {season}: suspiciously small file")
    path = cache.put_bytes(season, "sportsdataverse", key, "parquet", payload)
    log.info("sportsdataverse.downloaded", asset=asset, season=season, bytes=len(payload))
    return path


def read_parquet(path: Path, columns: list[str] | None = None) -> pl.DataFrame:
    """Read selected columns, tolerating columns missing from older seasons."""
    if columns is None:
        return pl.read_parquet(path)
    available = set(pl.read_parquet_schema(path).keys())
    keep = [c for c in columns if c in available]
    missing = [c for c in columns if c not in available]
    if missing:
        log.warning("sportsdataverse.missing_columns", path=str(path), missing=missing)
    return pl.read_parquet(path, columns=keep)


def load_pbp(
    cache: DiskCache, ttl: CacheTTL, cfg: SportsdataverseConfig, season: int
) -> pl.DataFrame:
    df = read_parquet(fetch_parquet(cache, ttl, cfg, season, "pbp"), PBP_COLUMNS)
    log.info("sportsdataverse.pbp", season=season, plays=df.height, games=df["game_id"].n_unique())
    return df


def load_drives(
    cache: DiskCache, ttl: CacheTTL, cfg: SportsdataverseConfig, season: int
) -> pl.DataFrame:
    return read_parquet(fetch_parquet(cache, ttl, cfg, season, "drives"))
