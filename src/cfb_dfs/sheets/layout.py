"""Tab specifications: names, headers, freeze panes, visibility, protection."""

from __future__ import annotations

from dataclasses import dataclass, field

from cfb_dfs.config import TabNames


@dataclass(frozen=True)
class TabSpec:
    name: str
    header: list[str]
    frozen_rows: int = 1
    frozen_cols: int = 0
    hidden: bool = False
    protected: bool = False
    header_row: int = 1  # 1-based row that holds the header (row 1 may be a title/status line)
    notes: list[str] = field(default_factory=list)  # written above the header when header_row > 1


MAIN_HEADER = [
    "Slate",
    "Kickoff (CT)",
    "Team",
    "Total",
    "Spread",
    "ITT",
    "Temp °F",
    "Precip %",
    "Wind mph",
    "Weather",
    "Pace (plays/min)",
    "Pace Rk",
    "Off EPA/DB",
    "Rk",
    "Off EPA/Rush",
    "Rk",
    "RROE%",
    "Rk",
    "Def EPA/DB",
    "Rk",
    "Def EPA/Rush",
    "Rk",
    "Pace L3",
    "Off EPA/DB L3",
    "Off EPA/Rush L3",
    "Def EPA/DB L3",
    "Def EPA/Rush L3",
    "All slates",
    "Book",
    "School",
    "Conf",
    "Class",
    "Game id",
    "Slate source",
]

VEGAS_RAW_HEADER = [
    "Season",
    "Week",
    "Game id",
    "Kickoff (CT)",
    "Team id",
    "Team",
    "School",
    "Opp id",
    "Opp",
    "H/A",
    "Book",
    "Total",
    "Spread",
    "ITT",
    "Spread open",
    "Total open",
    "Moneyline",
    "Books available",
    "Slates",
    "Fetched (CT)",
]

PACE_RAW_HEADER = [
    "Team id",
    "Team",
    "School",
    "Conf",
    "Games",
    "Plays/min (neutral)",
    "Rank",
    "Sec/play (neutral)",
    "Neutral plays",
    "Neutral minutes",
    "Plays/min L3",
    "Rank L3",
    "Games L3",
    "Plays/min (all situations)",
    "All plays",
    "All minutes",
    "Drives",
]

EPA_RAW_HEADER = [
    "Team id",
    "Team",
    "School",
    "Conf",
    "Games",
    "Off EPA/DB",
    "Rk",
    "Off EPA/Rush",
    "Rk",
    "Off EPA/Play",
    "Rk",
    "Off dropbacks",
    "Off rushes",
    "Def EPA/DB",
    "Rk",
    "Def EPA/Rush",
    "Rk",
    "Def EPA/Play",
    "Rk",
    "Def dropbacks",
    "Def rushes",
    "Off EPA/DB L3",
    "Rk",
    "Off EPA/Rush L3",
    "Rk",
    "Def EPA/DB L3",
    "Rk",
    "Def EPA/Rush L3",
    "Rk",
    "CFBD PPA off pass",
    "CFBD PPA off rush",
    "CFBD PPA def pass",
    "CFBD PPA def rush",
]

WEATHER_RAW_HEADER = [
    "Season",
    "Week",
    "Game id",
    "Kickoff (CT)",
    "Away",
    "Home",
    "Venue",
    "City",
    "State",
    "Dome",
    "Temp °F (kickoff)",
    "Precip % (max, game window)",
    "Precip in (game window)",
    "Wind mph (mean)",
    "Gust mph (max)",
    "Condition",
    "Note",
    "Fetched (CT)",
]

RROE_RAW_HEADER = [
    "Team id",
    "Team",
    "School",
    "Conf",
    "Games",
    "Early-down plays",
    "Rush rate",
    "Expected rush rate",
    "RROE%",
    "Rank",
    "RROE% L3",
    "Rank L3",
    "Plays L3",
    "Note",
]

TEAMS_HEADER = [
    "Team id",
    "Abbrev",
    "School",
    "Mascot",
    "Conference",
    "Class",
    "CFBD abbrev",
    "Logo",
]

CONFIG_HEADER = [
    "Slate name",
    "Teams (abbrevs, comma-separated)",
    "Weekday",
    "Start (HH:MM CT)",
    "End (HH:MM CT)",
]
CONFIG_NOTES = [
    "Manual slate overrides. Fill a row to define a slate when DraftKings detection fails "
    "(or to override a DK slate with the same name). Use EITHER a team list OR a weekday + window.",
    "Example: Main | BAMA, UK, OU, MICH | | |     or     Night | | Sat | 18:00 | 20:59",
]

LOG_HEADER = [
    "Timestamp (CT)",
    "Season",
    "Week",
    "Status",
    "Stages OK",
    "Stages failed",
    "Rows per tab",
    "Sources",
    "Warnings",
    "Duration (s)",
    "CFBD calls",
    "Dry run",
    "Git",
    "Runner",
]


def build_specs(tabs: TabNames) -> dict[str, TabSpec]:
    return {
        "main": TabSpec(tabs.main, MAIN_HEADER, frozen_rows=2, frozen_cols=3, header_row=2),
        "vegas_raw": TabSpec(tabs.vegas_raw, VEGAS_RAW_HEADER, frozen_cols=6, protected=True),
        "pace_raw": TabSpec(
            tabs.pace_raw,
            PACE_RAW_HEADER,
            frozen_rows=2,
            frozen_cols=3,
            header_row=2,
            protected=True,
        ),
        "weather_raw": TabSpec(tabs.weather_raw, WEATHER_RAW_HEADER, frozen_cols=6, protected=True),
        "rroe_raw": TabSpec(
            tabs.rroe_raw,
            RROE_RAW_HEADER,
            frozen_rows=2,
            frozen_cols=3,
            header_row=2,
            protected=True,
        ),
        "epa_raw": TabSpec(
            tabs.epa_raw, EPA_RAW_HEADER, frozen_rows=2, frozen_cols=3, header_row=2, protected=True
        ),
        "teams": TabSpec(tabs.teams, TEAMS_HEADER, frozen_cols=2, protected=True),
        "config": TabSpec(
            tabs.config, CONFIG_HEADER, frozen_rows=3, header_row=3, notes=CONFIG_NOTES
        ),
        "log": TabSpec(tabs.log, LOG_HEADER, hidden=True),
    }
