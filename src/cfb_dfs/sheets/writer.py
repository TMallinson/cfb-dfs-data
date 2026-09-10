"""Batched, idempotent writes: ensure tabs exist, replace table bodies, diff for dry runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cfb_dfs.logging_setup import get_logger
from cfb_dfs.sheets.client import SheetsClient
from cfb_dfs.sheets.layout import TabSpec

log = get_logger(__name__)

Cell = str | float | int | bool | None
MAX_COLS = 60


def col_letter(idx: int) -> str:
    """0-based column index -> A1 letters."""
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def a1(tab: str, rng: str) -> str:
    return f"'{tab.replace(chr(39), chr(39) * 2)}'!{rng}"


@dataclass
class TableWrite:
    spec: TabSpec
    rows: list[list[Cell]]
    title: str | None = None  # goes into A1 when header_row > 1

    @property
    def body_start_row(self) -> int:
        return self.spec.header_row + 1

    def values(self) -> list[list[Cell]]:
        """Everything from row 1 down: title/notes, header, body."""
        pre: list[list[Cell]] = []
        if self.spec.header_row > 1:
            first: list[Cell] = [self.title or ""]
            pre.append(first)
            for note in self.spec.notes[: self.spec.header_row - 2]:
                pre.append([note])
            while len(pre) < self.spec.header_row - 1:
                pre.append([""])
        return [*pre, list(self.spec.header), *self.rows]


@dataclass
class WriteReport:
    rows_written: dict[str, int] = field(default_factory=dict)
    tabs_created: list[str] = field(default_factory=list)
    tabs_deleted: list[str] = field(default_factory=list)
    diff: dict[str, str] = field(default_factory=dict)
    requests: int = 0


class SheetWriter:
    def __init__(self, client: SheetsClient) -> None:
        self.client = client
        self._meta: dict[str, Any] | None = None

    # -- metadata --------------------------------------------------------------------------

    def metadata(self, refresh: bool = False) -> dict[str, Any]:
        if self._meta is None or refresh:
            self._meta = self.client.get_metadata(
                fields=(
                    "sheets(properties(sheetId,title,index,hidden,gridProperties),"
                    "protectedRanges,conditionalFormats)"
                )
            )
        return self._meta

    def sheet_ids(self) -> dict[str, int]:
        return {
            s["properties"]["title"]: s["properties"]["sheetId"]
            for s in self.metadata().get("sheets", [])
        }

    # -- structure -------------------------------------------------------------------------

    def ensure_tabs(self, specs: list[TabSpec], dry_run: bool = False) -> list[str]:
        existing = self.sheet_ids()
        missing = [s for s in specs if s.name not in existing]
        if not missing:
            return []
        if dry_run:
            log.info("sheets.would_create_tabs", tabs=[s.name for s in missing])
            return [s.name for s in missing]
        requests = []
        for s in missing:
            requests.append(
                {
                    "addSheet": {
                        "properties": {
                            "title": s.name,
                            "hidden": s.hidden,
                            "gridProperties": {
                                "rowCount": 1000,
                                "columnCount": MAX_COLS,
                                "frozenRowCount": s.frozen_rows,
                                "frozenColumnCount": s.frozen_cols,
                            },
                        }
                    }
                }
            )
        self.client.batch_update(requests)
        self.metadata(refresh=True)
        log.info("sheets.created_tabs", tabs=[s.name for s in missing])
        return [s.name for s in missing]

    def apply_properties(self, specs: list[TabSpec]) -> None:
        """Freeze panes / hidden flags for existing tabs (idempotent)."""
        ids = self.sheet_ids()
        requests = []
        for s in specs:
            if s.name not in ids:
                continue
            requests.append(
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": ids[s.name],
                            "hidden": s.hidden,
                            "gridProperties": {
                                "frozenRowCount": s.frozen_rows,
                                "frozenColumnCount": s.frozen_cols,
                            },
                        },
                        "fields": (
                            "hidden,gridProperties.frozenRowCount,gridProperties.frozenColumnCount"
                        ),
                    }
                }
            )
            requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": ids[s.name],
                            "startRowIndex": s.header_row - 1,
                            "endRowIndex": s.header_row,
                            "startColumnIndex": 0,
                            "endColumnIndex": len(s.header),
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "textFormat": {"bold": True},
                                "backgroundColor": {"red": 0.93, "green": 0.93, "blue": 0.93},
                            }
                        },
                        "fields": "userEnteredFormat(textFormat,backgroundColor)",
                    }
                }
            )
        self.client.batch_update(requests)

    def protected_tabs(self) -> set[str]:
        return {
            s["properties"]["title"]
            for s in self.metadata().get("sheets", [])
            if s.get("protectedRanges")
        }

    def format_main(self, spec: TabSpec) -> None:
        """Replace the main tab's conditional formats, number formats, column widths."""
        from cfb_dfs.sheets.format import (
            column_width_requests,
            conditional_format_requests,
            number_format_requests,
        )

        meta = self.metadata(refresh=True)
        sheet = next(
            (s for s in meta.get("sheets", []) if s["properties"]["title"] == spec.name), None
        )
        if sheet is None:
            return
        sid = sheet["properties"]["sheetId"]
        existing = len(sheet.get("conditionalFormats", []))
        reqs = conditional_format_requests(sid, spec.header, spec.header_row, existing)
        reqs += number_format_requests(sid, spec.header, spec.header_row)
        reqs += column_width_requests(sid, spec.header)
        self.client.batch_update(reqs)
        log.info("sheets.formatted", tab=spec.name, rules_replaced=existing)

    def delete_tabs(self, titles: list[str], dry_run: bool = False) -> list[str]:
        """Delete tabs. Tabs with owner-only protections cannot be deleted by the
        service account; those are hidden instead and reported."""
        ids = self.sheet_ids()
        protected = self.protected_tabs()
        targets = [t for t in titles if t in ids and t not in protected]
        blocked = [t for t in titles if t in ids and t in protected]
        if dry_run:
            log.info("sheets.would_delete_tabs", tabs=targets, blocked_by_protection=blocked)
            return targets
        if targets:
            self.client.batch_update([{"deleteSheet": {"sheetId": ids[t]}} for t in targets])
            log.info("sheets.deleted_tabs", tabs=targets)
        if blocked:
            try:
                self.client.batch_update(
                    [
                        {
                            "updateSheetProperties": {
                                "properties": {"sheetId": ids[t], "hidden": True},
                                "fields": "hidden",
                            }
                        }
                        for t in blocked
                    ]
                )
                log.warning("sheets.protected_tabs_hidden_not_deleted", tabs=blocked)
            except Exception:
                log.warning("sheets.protected_tabs_left_in_place", tabs=blocked)
        self.metadata(refresh=True)
        return targets

    # -- values ----------------------------------------------------------------------------

    def write_tables(self, tables: list[TableWrite], dry_run: bool = False) -> WriteReport:
        report = WriteReport()
        if not tables:
            return report
        clear_ranges = [a1(t.spec.name, f"A1:{col_letter(MAX_COLS - 1)}") for t in tables]
        data = []
        for t in tables:
            values = t.values()
            data.append({"range": a1(t.spec.name, "A1"), "values": _sanitize(values)})
            report.rows_written[t.spec.name] = len(t.rows)
        if dry_run:
            report.diff = self.diff(tables)
            return report
        self.client.batch_clear_values(clear_ranges)
        self.client.batch_update_values(data, value_input="USER_ENTERED")
        report.requests = 2
        return report

    def read_table(self, spec: TabSpec) -> list[list[Any]]:
        """Existing body rows (below the header) or [] if the tab is missing."""
        if spec.name not in self.sheet_ids():
            return []
        rng = a1(spec.name, f"A{spec.header_row + 1}:{col_letter(len(spec.header) - 1)}")
        vr = self.client.batch_get_values([rng], render="UNFORMATTED_VALUE")
        return vr[0].get("values", []) if vr else []

    def append_rows(self, spec: TabSpec, rows: list[list[Cell]]) -> None:
        req = (
            self.client.service.spreadsheets()
            .values()
            .append(
                spreadsheetId=self.client.spreadsheet_id,
                range=a1(spec.name, "A1"),
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": _sanitize(rows)},
            )
        )
        self.client._execute(req, "write")

    def diff(self, tables: list[TableWrite]) -> dict[str, str]:
        """Compare intended values with the sheet; returns a one-line summary per tab."""
        ids = self.sheet_ids()
        out: dict[str, str] = {}
        present = [t for t in tables if t.spec.name in ids]
        ranges = [a1(t.spec.name, f"A1:{col_letter(MAX_COLS - 1)}") for t in present]
        current = self.client.batch_get_values(ranges, render="UNFORMATTED_VALUE") if ranges else []
        cur_by_tab = {
            t.spec.name: vr.get("values", []) for t, vr in zip(present, current, strict=False)
        }
        for t in tables:
            new = _sanitize(t.values())
            if t.spec.name not in ids:
                out[t.spec.name] = f"tab missing; would create and write {len(t.rows)} rows"
                continue
            old = cur_by_tab.get(t.spec.name, [])
            changed = 0
            samples: list[str] = []
            for r in range(max(len(old), len(new))):
                orow = old[r] if r < len(old) else []
                nrow = new[r] if r < len(new) else []
                for c in range(max(len(orow), len(nrow))):
                    ov = orow[c] if c < len(orow) else ""
                    nv = nrow[c] if c < len(nrow) else ""
                    if _norm(ov) != _norm(nv):
                        changed += 1
                        if len(samples) < 5:
                            samples.append(f"{col_letter(c)}{r + 1}: {ov!r} -> {nv!r}")
            out[t.spec.name] = f"{changed} cell(s) differ; {len(old)} -> {len(new)} rows" + (
                f"; e.g. {'; '.join(samples)}" if samples else ""
            )
        return out


def _norm(v: Any) -> Any:
    if v in (None, ""):
        return ""
    if isinstance(v, bool):
        return v
    if isinstance(v, int | float):
        return round(float(v), 6)
    return str(v)


def _sanitize(values: list[list[Cell]]) -> list[list[Any]]:
    out: list[list[Any]] = []
    for row in values:
        out.append(["" if v is None else v for v in row])
    return out
