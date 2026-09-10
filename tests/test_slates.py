from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from cfb_dfs.config import KickoffWindow
from cfb_dfs.models import Game, Team
from cfb_dfs.models.slates import DkGame, DkTeam, Slate
from cfb_dfs.sources.draftkings import _parse_competition, _parse_dk_time, slate_label
from cfb_dfs.transform.slates import assign, kickoff_window_label, manual_slates_from_rows
from cfb_dfs.transform.teams import Overrides, TeamIndex

CT = ZoneInfo("America/Chicago")
REPO = Path(__file__).resolve().parents[1]
WINDOWS = [
    KickoffWindow(name="Main", weekday="Sat", start="11:00", end="14:29"),
    KickoffWindow(name="Afternoon", weekday="Sat", start="14:30", end="17:59"),
    KickoffWindow(name="Night", weekday="Sat", start="18:00", end="20:59"),
    KickoffWindow(name="Late Night", weekday="Sat", start="21:00", end="23:59"),
]


def _setup(load_json):
    teams = [Team.model_validate(t) for t in load_json("cfbd_teams_subset.json")]
    idx = TeamIndex(teams, Overrides.load(REPO / "data/overrides/team_aliases.yaml"))
    games = [Game.model_validate(g) for g in load_json("cfbd_games_2026_w2_subset.json")]
    return idx, games


def test_dk_time_and_labels():
    t = _parse_dk_time("2026-09-12T16:00:00.0000000Z")
    assert t == datetime(2026, 9, 12, 16, tzinfo=UTC)
    assert slate_label("Classic", "", t) == "Main"
    assert slate_label("Classic", " (Afternoon)", t) == "Afternoon"
    assert slate_label("Classic", None, datetime(2026, 9, 11, 23, 30, tzinfo=UTC)) == "Fri"
    assert slate_label("Showdown", " (OU @ MICH)", t) == "SHW OU@MICH"


def test_draftables_parse(load_json):
    comps = load_json("dk_draftables_153231.json")["competitions"]
    games = [_parse_competition(c) for c in comps]
    assert len(games) == 12
    psu = next(g for g in games if g.name == "PSU @ TEMP")
    assert psu.away.city == "Penn State" and psu.home.abbreviation == "TEMP"


def test_assign_from_dk_slates(load_json):
    idx, games = _setup(load_json)
    comps = load_json("dk_draftables_153231.json")["competitions"]
    main = Slate(
        slate_id="153231",
        label="Main",
        game_type="Classic",
        start_time=datetime(2026, 9, 12, 16, tzinfo=UTC),
        games=[_parse_competition(c) for c in comps],
        source="draftkings",
    )
    shw = Slate(
        slate_id="153317",
        label="SHW OU@MICH",
        game_type="Showdown",
        start_time=datetime(2026, 9, 12, 16, tzinfo=UTC),
        games=[g for g in main.games if g.name == "OU @ MICH"],
        source="draftkings",
    )
    out, warnings = assign(games, [shw, main], [], WINDOWS, idx, CT)
    by_home = {gs.game.home_team: gs for gs in out}
    assert by_home["Michigan"].primary == "Main"
    assert by_home["Michigan"].labels == ["Main", "SHW OU@MICH"]
    assert by_home["Temple"].primary == "Main"
    # Games not on any DK slate are excluded when slates exist (OSU @ TEX is a night game)
    assert "Texas" not in by_home
    assert all("not in CFBD week schedule" in w or "could not map" in w for w in warnings)


def test_kickoff_window_fallback(load_json):
    idx, games = _setup(load_json)
    out, warnings = assign(games, [], [], WINDOWS, idx, CT)
    assert len(out) == len(games)
    assert any("kickoff-window" in w for w in warnings)
    tex = next(gs for gs in out if gs.game.home_team == "Texas")
    assert tex.primary in {"Main", "Afternoon", "Night", "Late Night", "Sat"}
    assert kickoff_window_label(datetime(2026, 9, 12, 16, tzinfo=UTC), WINDOWS, CT) == "Main"
    assert kickoff_window_label(datetime(2026, 9, 11, 0, tzinfo=UTC), WINDOWS, CT) == "Thu"


def test_manual_config_rows(load_json):
    idx, games = _setup(load_json)
    rows = [
        ["Slate name", "Teams", "Weekday", "Start", "End"],
        ["My Slate", "TEM, MICH", "", "", ""],
        ["Sat Early", "", "Sat", "10:00", "12:00"],
        ["", "", "", "", ""],
    ]
    slates = manual_slates_from_rows(rows, games, idx, CT)
    labels = {s.label: s for s in slates}
    assert {g.name for g in labels["My Slate"].games} == {"PSU @ TEM", "OU @ MICH"}
    assert len(labels["My Slate"].games) == 2
    assert labels["My Slate"].source == "config"


def test_dk_game_type_ids_are_documented():
    from cfb_dfs.sources.draftkings import GAME_TYPES, SKIP_GAME_TYPES

    assert GAME_TYPES[94] == "Classic" and GAME_TYPES[95] == "Showdown"
    assert "Snake" in SKIP_GAME_TYPES
    DkGame(
        name="x",
        start_time=datetime.now(UTC),
        home=DkTeam(city="a", abbreviation="A"),
        away=DkTeam(city="b", abbreviation="B"),
    )
