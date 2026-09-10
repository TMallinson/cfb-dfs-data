from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl

from cfb_dfs.config import MetricsConfig
from cfb_dfs.models import Game, Team
from cfb_dfs.sheets.format import (
    MAIN_RULES,
    column_width_requests,
    conditional_format_requests,
    number_format_requests,
)
from cfb_dfs.sheets.layout import MAIN_HEADER
from cfb_dfs.transform.filters import prepare_plays
from cfb_dfs.transform.plays import normalize_plays
from cfb_dfs.transform.rroe import FEATURES, feature_frame, lines_frame, team_rroe, train
from cfb_dfs.transform.teams import TeamIndex
from cfb_dfs.transform.weather import Venue, summarize, venues_from_rows


def _game(start: datetime, venue_id: int | None = 1) -> Game:
    return Game.model_validate(
        {
            "id": 1,
            "season": 2026,
            "week": 2,
            "seasonType": "regular",
            "startDate": start.isoformat(),
            "homeId": 10,
            "homeTeam": "H",
            "awayId": 20,
            "awayTeam": "A",
            "venueId": venue_id,
            "venue": "Stadium",
        }
    )


def _forecast(start: datetime) -> dict:
    hours = [start.replace(minute=0) + timedelta(hours=h) for h in range(-2, 6)]
    return {
        "hourly": {
            "time": [h.strftime("%Y-%m-%dT%H:00") for h in hours],
            "temperature_2m": [70, 71, 72, 73, 74, 75, 76, 77],
            "precipitation_probability": [0, 5, 10, 60, 20, 5, 0, 0],
            "precipitation": [0, 0, 0.1, 0.2, 0, 0, 0, 0],
            "wind_speed_10m": [5, 5, 10, 12, 14, 8, 6, 6],
            "wind_gusts_10m": [8, 9, 15, 25, 20, 10, 9, 9],
            "weather_code": [0, 1, 2, 61, 3, 1, 0, 0],
        }
    }


def test_weather_summary_window():
    kick = datetime(2026, 9, 12, 16, 0, tzinfo=UTC)
    v = Venue(1, "Stadium", "X", "MI", 42.0, -83.0, dome=False)
    w = summarize(_game(kick), v, _forecast(kick))
    assert w.temp_f == 72 and w.precip_prob == 60 and w.precip_in == 0.3
    assert w.wind_mph == 11 and w.gust_mph == 25 and w.condition == "Partly cloudy"
    assert w.note is None


def test_weather_dome_and_missing():
    kick = datetime(2026, 9, 12, 16, 0, tzinfo=UTC)
    dome = Venue(2, "Dome", "X", "GA", 33.0, -84.0, dome=True)
    assert summarize(_game(kick), dome, None).condition == "Dome"
    assert summarize(_game(kick), None, None).note == "no venue coordinates"
    far = _game(kick + timedelta(days=30))
    assert (
        summarize(far, Venue(1, "S", None, None, 1.0, 1.0, False), _forecast(kick)).note
        == "beyond forecast"
    )
    vs = venues_from_rows(
        [{"id": 5, "name": "N", "latitude": "1.5", "longitude": None, "dome": None}]
    )
    assert vs[5].latitude == 1.5 and vs[5].longitude is None and vs[5].dome is False


def test_format_requests_cover_every_rule_and_are_idempotent():
    reqs = conditional_format_requests(7, MAIN_HEADER, header_row=2, existing_rule_count=3)
    deletes = [r for r in reqs if "deleteConditionalFormatRule" in r]
    adds = [r for r in reqs if "addConditionalFormatRule" in r]
    assert len(deletes) == 3
    n_rk = MAIN_HEADER.count("Rk")
    present = [r for r in MAIN_RULES if r.header in MAIN_HEADER]
    assert len(adds) == len(present) + n_rk
    for a in adds:
        rng = a["addConditionalFormatRule"]["rule"]["ranges"][0]
        assert rng["startRowIndex"] == 2 and rng["endColumnIndex"] == rng["startColumnIndex"] + 1
    assert (
        len(number_format_requests(7, MAIN_HEADER, 2))
        == len([r for r in present if r.number_format]) + n_rk
    )
    assert len(column_width_requests(7, MAIN_HEADER)) == len(MAIN_HEADER)


def _rroe_rows(load_json) -> tuple[pl.DataFrame, TeamIndex]:
    teams = TeamIndex([Team.model_validate(t) for t in load_json("cfbd_teams_subset.json")])
    plays = prepare_plays(
        normalize_plays(load_json("cfbd_plays_2026_w1_sample.json"), teams, 1), MetricsConfig()
    )
    lines = lines_frame(load_json("cfbd_lines_2026_w2.json"), ["DraftKings"])
    return feature_frame(plays, lines), teams


def test_feature_frame_and_lines(load_json):
    rows, _ = _rroe_rows(load_json)
    assert set(FEATURES) <= set(rows.columns) and "y" in rows.columns
    assert rows["y"].mean() > 0.3 and rows["y"].mean() < 0.8
    assert rows.filter(pl.col("down").is_between(1, 4)).height == rows.height
    assert rows["has_line"].sum() == 0  # week-2 lines don't cover these week-1 games
    ln = lines_frame(load_json("cfbd_lines_2026_w2.json"), ["DraftKings"])
    ku = ln.filter(pl.col("game_id") == 401856678).row(0, named=True)
    assert ku["home_spread"] == 5.5 and ku["total"] == 50.5


def test_train_and_team_rroe_on_small_sample(load_json):
    rows, teams = _rroe_rows(load_json)
    # Duplicate games with new ids so the grouped holdout has something to hold out.
    big = pl.concat([rows.with_columns((pl.col("game_id") + k).alias("game_id")) for k in range(6)])
    model, rep = train(big, holdout_frac=0.2, seed=1)
    assert rep.n_holdout > 0 and rep.n_train > rep.n_holdout
    assert 0 < rep.log_loss_model < 1 and len(rep.calibration) >= 3
    out = team_rroe(model, rows, {401856634: 0, 401856777: 1, 401858423: 2}, teams.fbs_ids())
    assert out.height == 6 and out["rroe"].null_count() == 0
    assert sorted(out["rroe_rank"].drop_nulls().to_list()) == [1, 2, 3, 4, 5, 6]
    assert np.isclose(out["rush_rate"].mean(), out["exp_rush_rate"].mean(), atol=0.25)
