from __future__ import annotations

from cfb_dfs.pipeline import merge_vegas_history
from cfb_dfs.sheets.layout import TabSpec
from cfb_dfs.sheets.writer import TableWrite, col_letter


def test_col_letter():
    assert col_letter(0) == "A" and col_letter(25) == "Z" and col_letter(26) == "AA"
    assert col_letter(59) == "BH"


def test_table_values_with_title_row():
    spec = TabSpec("T", ["a", "b"], header_row=2)
    tw = TableWrite(spec, [[1, 2]], title="Last updated")
    assert tw.values() == [["Last updated"], ["a", "b"], [1, 2]]
    assert TableWrite(TabSpec("U", ["x"]), [[None]]).values() == [["x"], [None]]


def test_merge_vegas_history_replaces_only_current_week():
    existing = [[2026, 1, 1, "Sat 09/05 11:00 AM"], [2026, 2, 9, "old"], ["junk"]]
    new = [[2026, 2, 5, "Sat 09/12 11:00 AM"]]
    merged = merge_vegas_history(existing, new, week=2, season=2026)
    assert merged == [[2026, 1, 1, "Sat 09/05 11:00 AM"], [2026, 2, 5, "Sat 09/12 11:00 AM"]]
