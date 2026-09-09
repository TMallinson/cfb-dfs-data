"""Read-only dump of a spreadsheet's structure as Markdown."""

from __future__ import annotations

import json
from typing import Any

from cfb_dfs.sheets.client import SheetsClient

HEADER_ROWS = 4


def _a1(title: str, rng: str) -> str:
    return f"'{title.replace(chr(39), chr(39) * 2)}'!{rng}"


def _grid_range_a1(gr: dict[str, Any]) -> str:
    def col(i: int) -> str:
        s = ""
        i += 1
        while i:
            i, r = divmod(i - 1, 26)
            s = chr(65 + r) + s
        return s

    sr, er = gr.get("startRowIndex"), gr.get("endRowIndex")
    sc, ec = gr.get("startColumnIndex"), gr.get("endColumnIndex")
    if sr is None and sc is None:
        return "whole sheet"
    start = f"{col(sc) if sc is not None else ''}{sr + 1 if sr is not None else ''}"
    end = f"{col(ec - 1) if ec is not None else ''}{er if er is not None else ''}"
    return f"{start}:{end}"


def inspect_spreadsheet(client: SheetsClient) -> tuple[dict[str, Any], str]:
    """Return (raw structure dict, markdown report). Makes 3 read requests."""
    meta = client.get_metadata(
        fields="properties(title,timeZone,locale),spreadsheetId,"
        "sheets(properties,protectedRanges,conditionalFormats,merges)"
    )
    sheets = meta.get("sheets", [])
    titles = [s["properties"]["title"] for s in sheets]

    ranges = [_a1(t, f"A1:AZ{HEADER_ROWS}") for t in titles]
    formulas = client.batch_get_values(ranges, render="FORMULA")
    grid = client.get_metadata(
        fields="sheets(properties.title,data(startRow,startColumn,"
        "rowData(values(dataValidation))))",
        ranges=ranges,
        include_grid_data=True,
    )
    validations: dict[str, list[str]] = {}
    for s in grid.get("sheets", []):
        title = s["properties"]["title"]
        for gd in s.get("data", []):
            r0, c0 = gd.get("startRow", 0), gd.get("startColumn", 0)
            for r, row in enumerate(gd.get("rowData", [])):
                for c, cell in enumerate(row.get("values", [])):
                    dv = cell.get("dataValidation")
                    if dv:
                        cond = dv.get("condition", {})
                        vals = [v.get("userEnteredValue") for v in cond.get("values", [])]
                        validations.setdefault(title, []).append(
                            f"R{r0 + r + 1}C{c0 + c + 1}: {cond.get('type')} {vals}"
                        )

    lines: list[str] = []
    p = meta.get("properties", {})
    lines.append(f"# Sheet structure: {p.get('title')}")
    lines.append("")
    lines.append(f"- Spreadsheet id: `{meta.get('spreadsheetId')}`")
    lines.append(f"- Timezone: {p.get('timeZone')} · Locale: {p.get('locale')}")
    hidden = sum(1 for s in sheets if s["properties"].get("hidden"))
    lines.append(f"- Tabs: {len(sheets)} ({hidden} hidden)")
    lines.append("")
    lines.append("| # | Tab | Size | Frozen | Hidden | Protected | Cond. formats | Merges |")
    lines.append("|---|-----|------|--------|--------|-----------|---------------|--------|")
    for s in sheets:
        pr = s["properties"]
        g = pr.get("gridProperties", {})
        lines.append(
            f"| {pr['index']} | `{pr['title']}` | {g.get('rowCount')}x{g.get('columnCount')} "
            f"| {g.get('frozenRowCount', 0)}r/{g.get('frozenColumnCount', 0)}c "
            f"| {'yes' if pr.get('hidden') else ''} | {len(s.get('protectedRanges', []))} "
            f"| {len(s.get('conditionalFormats', []))} | {len(s.get('merges', []))} |"
        )
    lines.append("")

    for s, vr in zip(sheets, formulas, strict=False):
        pr = s["properties"]
        title = pr["title"]
        lines.append(f"## `{title}`")
        for prot in s.get("protectedRanges", []):
            editors = prot.get("editors", {})
            n_users = len(editors.get("users", []))
            lines.append(
                f"- Protected: {_grid_range_a1(prot.get('range', {}))} · "
                f"{'warning only' if prot.get('warningOnly') else 'enforced'} · "
                f"{n_users} listed editor(s) · description: {prot.get('description', '')!r}"
            )
        for dv in validations.get(title, []):
            lines.append(f"- Data validation {dv}")
        rows = vr.get("values", [])
        if rows:
            lines.append(f"- First {min(len(rows), HEADER_ROWS)} rows (formulas shown as entered):")
            lines.append("")
            lines.append("```")
            for i, row in enumerate(rows[:HEADER_ROWS], start=1):
                cells = [str(c)[:70] for c in row]
                lines.append(f"{i}: {cells}")
            lines.append("```")
        else:
            lines.append("- (empty header rows)")
        lines.append("")

    report = "\n".join(lines)
    headers = {t: vr.get("values", []) for t, vr in zip(titles, formulas, strict=False)}
    structure = {"meta": meta, "validations": validations, "headers": headers}
    return structure, report


def dump_json(structure: dict[str, Any]) -> str:
    return json.dumps(structure, indent=1)
