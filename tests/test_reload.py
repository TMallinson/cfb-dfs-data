from __future__ import annotations

from cfb_dfs.pipeline import EPA_MAP, PACE_MAP, sheet_rows_to_metrics
from cfb_dfs.sheets.layout import EPA_RAW_HEADER, PACE_RAW_HEADER


def test_pace_rows_roundtrip():
    rows = [[333, "ALA", "Alabama", "SEC", 1, 2.5, 3, 24.0, 40, 16, 2.4, 4, 1, 2.3, 60, 25, 12]]
    df = sheet_rows_to_metrics(PACE_RAW_HEADER, rows, PACE_MAP)
    r = df.row(0, named=True)
    assert r["team_id"] == 333 and r["plays_per_min"] == 2.5 and r["pace_rank"] == 3
    assert r["plays_per_min_l3"] == 2.4 and r["pace_rank_l3"] == 4


def test_epa_rows_take_following_rank_columns_and_skip_junk():
    row = [0] * len(EPA_RAW_HEADER)
    row[0] = 61
    row[EPA_RAW_HEADER.index("Off EPA/DB")] = 0.9
    row[EPA_RAW_HEADER.index("Off EPA/DB") + 1] = 9
    row[EPA_RAW_HEADER.index("Def EPA/Rush")] = -0.4
    row[EPA_RAW_HEADER.index("Def EPA/Rush") + 1] = "FCS"
    df = sheet_rows_to_metrics(EPA_RAW_HEADER, [row, ["not a team"]], EPA_MAP)
    r = df.row(0, named=True)
    assert df.height == 1 and r["off_epa_db"] == 0.9 and r["off_epa_db_rank"] == 9
    assert r["def_epa_rush"] == -0.4 and r["def_epa_rush_rank"] is None
    assert sheet_rows_to_metrics(EPA_RAW_HEADER, [], EPA_MAP) is None
