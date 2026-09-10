"""Conditional formatting and number formats for the main tab (idempotent)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

GREEN = {"red": 0.42, "green": 0.75, "blue": 0.45}
RED = {"red": 0.93, "green": 0.45, "blue": 0.42}
WHITE = {"red": 1, "green": 1, "blue": 1}
BLUE = {"red": 0.45, "green": 0.6, "blue": 0.9}
ORANGE = {"red": 0.98, "green": 0.72, "blue": 0.4}


@dataclass(frozen=True)
class ColorRule:
    header: str  # column header text in the tab's header row
    kind: str  # "high_good" | "low_good" | "diverging"
    number_format: str | None = None  # e.g. "0.00", "0"


MAIN_RULES: list[ColorRule] = [
    ColorRule("Total", "high_good", "0.0"),
    ColorRule("Spread", "low_good", "+0.0;-0.0;0"),
    ColorRule("ITT", "high_good", "0.0"),
    ColorRule("Temp °F", "diverging", "0"),
    ColorRule("Precip %", "low_good", "0"),
    ColorRule("Wind mph", "low_good", "0"),
    ColorRule("Pace (plays/min)", "high_good", "0.00"),
    ColorRule("Pace Rk", "low_good", "0"),
    ColorRule("Off EPA/DB", "high_good", "0.00"),
    ColorRule("Off EPA/Rush", "high_good", "0.00"),
    ColorRule("RROE%", "diverging", "+0.0;-0.0;0"),
    ColorRule("Def EPA/DB", "low_good", "0.00"),
    ColorRule("Def EPA/Rush", "low_good", "0.00"),
    ColorRule("Pace L3", "high_good", "0.00"),
    ColorRule("Off EPA/DB L3", "high_good", "0.00"),
    ColorRule("Off EPA/Rush L3", "high_good", "0.00"),
    ColorRule("Def EPA/DB L3", "low_good", "0.00"),
    ColorRule("Def EPA/Rush L3", "low_good", "0.00"),
]
RANK_HEADER = "Rk"  # every "Rk" column: 1 = green


def _grid(sheet_id: int, col: int, first_row: int, last_row: int) -> dict[str, Any]:
    return {
        "sheetId": sheet_id,
        "startRowIndex": first_row - 1,
        "endRowIndex": last_row,
        "startColumnIndex": col,
        "endColumnIndex": col + 1,
    }


def _gradient(kind: str) -> dict[str, Any]:
    if kind == "high_good":
        return {
            "minpoint": {"color": RED, "type": "MIN"},
            "midpoint": {"color": WHITE, "type": "PERCENTILE", "value": "50"},
            "maxpoint": {"color": GREEN, "type": "MAX"},
        }
    if kind == "low_good":
        return {
            "minpoint": {"color": GREEN, "type": "MIN"},
            "midpoint": {"color": WHITE, "type": "PERCENTILE", "value": "50"},
            "maxpoint": {"color": RED, "type": "MAX"},
        }
    return {
        "minpoint": {"color": ORANGE, "type": "MIN"},
        "midpoint": {"color": WHITE, "type": "PERCENTILE", "value": "50"},
        "maxpoint": {"color": BLUE, "type": "MAX"},
    }


def conditional_format_requests(
    sheet_id: int,
    header: list[str],
    header_row: int,
    existing_rule_count: int,
    last_row: int = 1000,
) -> list[dict[str, Any]]:
    """Delete every existing rule on the sheet, then add gradient rules per column."""
    reqs: list[dict[str, Any]] = [
        {"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": 0}}
        for _ in range(existing_rule_count)
    ]
    first_row = header_row + 1
    by_header = {h: i for i, h in enumerate(header)}
    for rule in MAIN_RULES:
        col = by_header.get(rule.header)
        if col is None:
            continue
        reqs.append(
            {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [_grid(sheet_id, col, first_row, last_row)],
                        "gradientRule": _gradient(rule.kind),
                    },
                    "index": 0,
                }
            }
        )
    for col, h in enumerate(header):
        if h == RANK_HEADER:
            reqs.append(
                {
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [_grid(sheet_id, col, first_row, last_row)],
                            "gradientRule": _gradient("low_good"),
                        },
                        "index": 0,
                    }
                }
            )
    return reqs


def number_format_requests(
    sheet_id: int, header: list[str], header_row: int, last_row: int = 1000
) -> list[dict[str, Any]]:
    reqs: list[dict[str, Any]] = []
    by_header = {h: i for i, h in enumerate(header)}
    fmts = {r.header: r.number_format for r in MAIN_RULES if r.number_format}
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
