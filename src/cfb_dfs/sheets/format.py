"""Conditional formatting and number formats for the main tab (idempotent).

Scales are normalized so outliers don't dominate: relative columns anchor their
colors at the 10th / 50th / 90th percentiles of the column instead of min / max.
Weather columns use fixed, research-based thresholds instead:

* Wind: passing efficiency declines measurably once sustained wind reaches
  ~15 mph and sharply above 20 mph (Advanced Football Analytics 2012, PFF 2017,
  Claremont/Wharton NFL studies: completion % falls from ~60% under 10 mph to
  ~55% at 20+ mph, with pass attempts dropping and rushes rising past 15 mph).
  So wind stays white through 12 mph, turns orange at 15, and is red at 20+.
* Temperature: blue when cold (<= 35 F), white around 68 F, red when hot (>= 95 F).
* Precipitation chance: white to 20%, orange at 50%, red at 80%+.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

GREEN = {"red": 0.42, "green": 0.75, "blue": 0.45}
RED = {"red": 0.93, "green": 0.45, "blue": 0.42}
WHITE = {"red": 1, "green": 1, "blue": 1}
BLUE = {"red": 0.45, "green": 0.6, "blue": 0.9}
ORANGE = {"red": 0.98, "green": 0.72, "blue": 0.4}

P_LOW, P_MID, P_HIGH = "10", "50", "90"


@dataclass(frozen=True)
class ColorRule:
    header: str
    kind: str  # high_good | low_good | temp | wind | precip
    number_format: str | None = None


MAIN_RULES: list[ColorRule] = [
    ColorRule("Total", "high_good", "0.0"),
    ColorRule("Spread", "low_good", "+0.0;-0.0;0"),
    ColorRule("ITT", "high_good", "0.0"),
    ColorRule("Temp °F", "temp", "0"),
    ColorRule("Precip %", "precip", "0"),
    ColorRule("Wind mph", "wind", "0"),
    ColorRule("Pace (plays/min)", "high_good", "0.00"),
    ColorRule("Pace Rk", "low_good", "0"),
    ColorRule("Off EPA/DB", "high_good", "0.00"),
    ColorRule("Off EPA/Rush", "high_good", "0.00"),
    # RROE: per request, pass-heavy (low RROE%, high rank number) is green.
    ColorRule("RROE%", "low_good", "+0.0;-0.0;0"),
    ColorRule("Def EPA/DB", "low_good", "0.00"),
    ColorRule("Def EPA/Rush", "low_good", "0.00"),
    ColorRule("Pace L3", "high_good", "0.00"),
    ColorRule("Off EPA/DB L3", "high_good", "0.00"),
    ColorRule("Off EPA/Rush L3", "high_good", "0.00"),
    ColorRule("Def EPA/DB L3", "low_good", "0.00"),
    ColorRule("Def EPA/Rush L3", "low_good", "0.00"),
]
RANK_HEADER = "Rk"
FLIPPED_RANK_AFTER = {"RROE%"}  # a "Rk" right after these headers: high number = green

# Player display tabs: volume/efficiency columns, higher = greener; weekly block as one scale.
TARGET_RULES: list[ColorRule] = [
    ColorRule(
        h,
        "high_good",
        "0.0" if h in ("Tgt/game", "Tgt/game L3", "Tgt share %", "aDOT", "Tgt/team DB") else "0",
    )
    for h in (
        "Targets",
        "Tgt/game",
        "Tgt L3",
        "Tgt/game L3",
        "Tgt share %",
        "Receptions",
        "Rec yards",
        "Rec TD",
        "aDOT",
        "Air yards",
        "YAC",
        "RZ targets",
        "Tgt/team DB",
    )
] + [ColorRule("PPA/tgt", "high_good", "0.00")]
PASSING_RULES: list[ColorRule] = [
    ColorRule(
        h, "high_good", "0.0" if h in ("Att/game", "Att/game L3", "Comp %", "YPA", "aDOT") else "0"
    )
    for h in (
        "Dropbacks",
        "Attempts",
        "Att/game",
        "Att L3",
        "Att/game L3",
        "Completions",
        "Comp %",
        "Pass yards",
        "YPA",
        "aDOT",
        "Pass TD",
        "Designed runs",
        "Rush yards",
    )
] + [
    ColorRule("INT", "low_good", "0"),
    ColorRule("Sacks", "low_good", "0"),
    ColorRule("PPA/att", "high_good", "0.00"),
]
RUSHING_RULES: list[ColorRule] = [
    ColorRule(
        h, "high_good", "0.0" if h in ("Car/game", "Car/game L3", "YPC", "Success %") else "0"
    )
    for h in (
        "Carries",
        "Car/game",
        "Car L3",
        "Car/game L3",
        "Rush yards",
        "YPC",
        "Rush TD",
        "Success %",
        "Targets",
        "Receptions",
        "Rec yards",
        "Touches",
    )
] + [ColorRule("PPA/carry", "high_good", "0.00")]
WEEK_BLOCK = ("Wk1", "Wk16")  # one gradient across all weekly columns


def _grid(sheet_id: int, col: int, first_row: int, last_row: int) -> dict[str, Any]:
    return {
        "sheetId": sheet_id,
        "startRowIndex": first_row - 1,
        "endRowIndex": last_row,
        "startColumnIndex": col,
        "endColumnIndex": col + 1,
    }


def _pct(lo: dict, mid: dict, hi: dict) -> dict[str, Any]:
    return {
        "minpoint": {"color": lo, "type": "PERCENTILE", "value": P_LOW},
        "midpoint": {"color": mid, "type": "PERCENTILE", "value": P_MID},
        "maxpoint": {"color": hi, "type": "PERCENTILE", "value": P_HIGH},
    }


def _num(lo: tuple[dict, float], mid: tuple[dict, float], hi: tuple[dict, float]) -> dict[str, Any]:
    return {
        "minpoint": {"color": lo[0], "type": "NUMBER", "value": str(lo[1])},
        "midpoint": {"color": mid[0], "type": "NUMBER", "value": str(mid[1])},
        "maxpoint": {"color": hi[0], "type": "NUMBER", "value": str(hi[1])},
    }


def gradient(kind: str) -> dict[str, Any]:
    if kind == "high_good":
        return _pct(RED, WHITE, GREEN)
    if kind == "low_good":
        return _pct(GREEN, WHITE, RED)
    if kind == "temp":
        return _num((BLUE, 35), (WHITE, 68), (RED, 95))
    if kind == "wind":
        return _num((WHITE, 12), (ORANGE, 15), (RED, 20))
    if kind == "precip":
        return _num((WHITE, 20), (ORANGE, 50), (RED, 80))
    raise ValueError(kind)


def _rank_kind(header: list[str], col: int) -> str:
    prev = header[col - 1] if col > 0 else ""
    return "high_good" if prev in FLIPPED_RANK_AFTER else "low_good"


def conditional_format_requests(
    sheet_id: int,
    header: list[str],
    header_row: int,
    existing_rule_count: int,
    last_row: int = 1000,
    rules: list[ColorRule] | None = None,
    week_block: bool = False,
) -> list[dict[str, Any]]:
    """Delete every existing rule on the sheet, then add gradient rules per column."""
    rules = MAIN_RULES if rules is None else rules
    reqs: list[dict[str, Any]] = [
        {"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": 0}}
        for _ in range(existing_rule_count)
    ]
    first_row = header_row + 1
    by_header = {h: i for i, h in enumerate(header)}

    def add(col: int, kind: str, end_col: int | None = None) -> None:
        rng = _grid(sheet_id, col, first_row, last_row)
        if end_col is not None:
            rng["endColumnIndex"] = end_col + 1
        reqs.append(
            {
                "addConditionalFormatRule": {
                    "rule": {"ranges": [rng], "gradientRule": gradient(kind)},
                    "index": 0,
                }
            }
        )

    for rule in rules:
        col = by_header.get(rule.header)
        if col is not None:
            add(col, rule.kind)
    for col, h in enumerate(header):
        if h == RANK_HEADER:
            add(col, _rank_kind(header, col))
    if week_block and WEEK_BLOCK[0] in by_header and WEEK_BLOCK[1] in by_header:
        add(by_header[WEEK_BLOCK[0]], "high_good", by_header[WEEK_BLOCK[1]])
    return reqs


def number_format_requests(
    sheet_id: int,
    header: list[str],
    header_row: int,
    last_row: int = 1000,
    rules: list[ColorRule] | None = None,
) -> list[dict[str, Any]]:
    rules = MAIN_RULES if rules is None else rules
    reqs: list[dict[str, Any]] = []
    by_header = {h: i for i, h in enumerate(header)}
    fmts = {r.header: r.number_format for r in rules if r.number_format}
    for h, fmt in fmts.items():
        col = by_header.get(h)
        if col is None:
            continue
        reqs.append(_number_format(sheet_id, col, header_row + 1, last_row, fmt))
    for col, h in enumerate(header):
        if h == RANK_HEADER:
            reqs.append(_number_format(sheet_id, col, header_row + 1, last_row, "0"))
    return reqs


def _number_format(
    sheet_id: int, col: int, first_row: int, last_row: int, fmt: str
) -> dict[str, Any]:
    return {
        "repeatCell": {
            "range": _grid(sheet_id, col, first_row, last_row),
            "cell": {
                "userEnteredFormat": {
                    "numberFormat": {"type": "NUMBER", "pattern": fmt},
                    "horizontalAlignment": "RIGHT",
                }
            },
            "fields": "userEnteredFormat(numberFormat,horizontalAlignment)",
        }
    }


def column_width_requests(sheet_id: int, header: list[str]) -> list[dict[str, Any]]:
    widths = {
        "Slate": 110,
        "Kickoff (CT)": 130,
        "Team": 70,
        "Weather": 110,
        "All slates": 160,
        "School": 150,
        "Conf": 130,
    }
    reqs = []
    for col, h in enumerate(header):
        w = widths.get(h, 72 if len(h) <= 6 else 95)
        reqs.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": col,
                        "endIndex": col + 1,
                    },
                    "properties": {"pixelSize": w},
                    "fields": "pixelSize",
                }
            }
        )
    return reqs
