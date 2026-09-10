# PLAN.md — architecture, layout, and build plan

Companion to RESEARCH.md. This is the proposal for approval before pipeline
code is written.

## 1. Architecture

```
                 ┌──────────────┐   ┌──────────────────┐   ┌──────────────────┐
  sources/       │ espn.py      │   │ sportsdataverse.py│   │ draftkings.py    │   cfbd.py (flagged,
  (raw fetch,    │ scoreboard + │   │ parquet releases │   │ lobby+draftables │   fallback/cross-check)
   disk cache)   │ DK lines     │   │ pbp/drives/teams │   │ slates → games   │
                 └──────┬───────┘   └────────┬─────────┘   └────────┬─────────┘
                        ▼                    ▼                      ▼
  models/        Game, Line, Team, Slate, Play (pydantic, typed, validated at the boundary)
                        ▼                    ▼                      ▼
  transform/     vegas.py  pace.py  epa.py  rroe.py  players.py  ranks.py  splits.py  filters.py
  (pure fns,     ─ implied totals   ─ garbage-time / kneel / spike / 2-min filters (one place)
   polars)       ─ season + last-3 windows for every metric   ─ FBS-only ranks, ties = min rank
                        ▼
  sheets/        layout.py (tab specs) → writer.py (batch values + batchUpdate, backoff, idempotent)
                 auth.py (file path or B64) · protect.py · log.py (hidden Log tab) · diff.py (--dry-run)
                        ▼
  pipeline.py    orchestrates stages, tolerates per-source failure, marks stale tabs, exit code
  cli.py         python -m cfb_dfs run --week 2 --only vegas,pace --dry-run --refresh --sheet-id
```

Principles: sources return raw typed records and never compute metrics;
transforms are pure functions over polars DataFrames with unit tests on small
fixtures; the sheet layer only knows about rectangular value blocks and named
ranges. One stage failing (e.g. DraftKings 403) never blocks the others.

## 2. Repo layout

```
cfb-dfs-data/
├── pyproject.toml            # uv-managed; ruff + pytest + mypy config
├── uv.lock
├── config.yaml               # season, week override, timezone, tab names, slate defs, feature flags
├── README.md  RESEARCH.md  PLAN.md
├── .github/workflows/pipeline.yml
├── src/cfb_dfs/
│   ├── __main__.py  cli.py  config.py  pipeline.py  logging_setup.py
│   ├── models/      teams.py  games.py  lines.py  slates.py  plays.py  metrics.py
│   ├── sources/     cache.py  espn.py  sportsdataverse.py  draftkings.py  cfbd.py  pff_csv.py
│   ├── transform/   filters.py  vegas.py  pace.py  epa.py  rroe.py  players.py  ranks.py  splits.py  teams.py
│   └── sheets/      auth.py  client.py  layout.py  writer.py  protect.py  log.py  diff.py  inspect.py
├── data/
│   ├── cache/                # gitignored; parquet/json keyed by season/week/endpoint
│   ├── manual/pff/           # optional CSV drop zone (gitignored contents)
│   └── overrides/team_aliases.yaml   # DK/ESPN/CFBD name overrides (committed)
├── reports/rroe_model_report.md      # holdout log loss + calibration table, regenerated each train
└── tests/
    ├── fixtures/             # small parquet/json slices from the real feeds
    ├── test_transform_*.py   # one per transform module
    ├── test_sources_*.py     # parsing against fixtures, no network
    └── test_e2e_fixtures.py  # shape + sanity: no nulls in key cols, ranks 1..N, ITT reconciles
```

## 3. Dependencies

Runtime: `polars`, `pyarrow`, `httpx`, `pydantic` v2, `pyyaml`, `google-auth`,
`google-api-python-client`, `scikit-learn` (logistic regression / gradient
boosting for RROE), `tenacity` (backoff), `structlog`, `typer` (CLI),
`python-dotenv` (local only).
Dev: `ruff`, `pytest`, `mypy`, `pytest-cov`.
Python 3.11+ (3.13 locally). `uv` installed today at `~/.local/bin/uv`; chosen
over Poetry for speed, lockfile, and Actions caching. pandas is not used.

## 4. Metric definitions (exact)

**Team key.** Canonical id = ESPN `team_id`. Display key = DK-style
abbreviation (from DK when present, else ESPN abbreviation, overridable in
`team_aliases.yaml`). Ref tabs carry `team_id`, `school`, `abbrev`, `conference`,
`classification`.

**Garbage time** (one function, used by pace/EPA/RROE): a play is garbage time
if the absolute score margin at the start of the play exceeds 28 in Q2, 21 in Q3,
or 14 in Q4 (Q1 never). Configurable in `config.yaml`; default is written into the
raw tabs' notes so the denominator is visible.

**Play filters** (`filters.py`): scrimmage plays only (`scrimmage_play`), drop
`penalty_no_play`, kneels (`kneel_down`), spikes (text matches `, Spike`),
timeouts/kickoffs/PAT/2-pt, and any play with clock ≤ 2:00 in period 2 or 4 for
the situation-neutral versions.

**Vegas.** `total = overUnder`; per-team `spread` (negative = favored, from ESPN
home-perspective spread); `implied_total = total/2 − spread/2`; `line_source`
column per row ("DraftKings via ESPN", or "CFBD:DraftKings"/"CFBD:consensus" on
fallback). Opening lines kept when the source provides them.

**Pace (revised 2026-09-09).** Drive-based, because play clocks are stale in
about half of all games: plays/min = scrimmage plays on qualifying drives ÷
drive elapsed minutes (CFBD `/drives`). Qualifying = not garbage time at drive
start, not starting inside the final 2:00 of a half, not an END OF HALF/GAME
kneel-out drive. Kneels, spikes, and no-play penalties are removed from play
counts. Seconds/play = 60 ÷ plays/min. All-situations plays/min is written
alongside. Rank 1 = fastest.

**EPA buckets.**
- Dropback = CFBD playType in {Pass Reception, Pass Incompletion, Passing
  Touchdown, Sack, Interception*}. No feed flags scrambles; QB rushes count as
  rushes (`include_scrambles_in_dropback` exists but is off by default and only
  works on feeds with passer names).
- Rush = playType in {Rush, Rushing Touchdown}, not a kneel. Fumble/safety plays
  count as plays for pace but sit in neither EPA bucket.
- EPA per play = CFBD `ppa`.
- Offense EPA/x = mean `EPA` over the team's offensive plays in the bucket;
  defense = mean `EPA` over plays where the team is `def_pos_team`. Offense rank
  1 = highest; defense rank 1 = lowest allowed.

**RROE%.** Model: gradient-boosted classifier (`HistGradientBoostingClassifier`)
predicting `P(rush)` from down, distance, yards to goal, score differential,
seconds remaining in half, half, offense timeouts left, plus `spread` and
`total` when available (pregame expectation is a strong pace/rush predictor).
Train on 2025 + 2026 non-garbage early-down plays; 20% game-grouped holdout;
report log loss, Brier, and a 10-bin calibration table in
`reports/rroe_model_report.md`. Team RROE% = mean(actual_rush − p_rush) × 100
over early-down (1st/2nd), non-garbage, situation-neutral plays. Rank 1 =
highest RROE (most run-heavy over expectation) — tell me if you want the
opposite.

**Splits.** Every team and player metric has `season` and `last3` (last three
games played by that team, by kickoff date). Both in raw tabs; main tab shows
season with `last3` as a secondary column.

**Ranks.** Over `classification == "fbs"` teams only; ties get the same (min)
rank; the denominator `N` is written to a cell on each raw tab header
("Rank of 136 FBS").

**FCS opponents on a slate** get a row with Vegas/ITT populated and blanks +
"FCS: not ranked" in rank columns.

## 5. Sheet plan (test sheet first; live only on your go-ahead)

Never delete tabs. Proposed writes:

| Tab | Action |
|-----|--------|
| `DK ` (main) | Keep header rows 1–3 and formatting. Rewrite A4:Q~ with **values** (not formulas): team abbrev, school, slate label, kickoff (CT), opponent, total, spread, ITT, pace + rank, off EPA/dropback + rank, off EPA/rush + rank, RROE% + rank, def EPA/dropback + rank, def EPA/rush + rank, last-3 secondary columns, line source. A1 = "Last updated: 2026-09-12 07:04 CDT · OK (vegas, pace, epa, rroe, players)". Existing VLOOKUP formulas in that range are replaced because they point at 2025 (ref) tabs; say so if you'd rather keep them. |
| `Vegas Raw`, `Pace Raw`, `EPA Raw`, `RROE Raw`, `Teams` | New clean raw tabs, all FBS + slate FCS teams, protected (your account as editor). |
| `Targets Raw`, `Passing Raw`, `Rushing Raw` | New per-player-season rows with weekly columns (Wk 1..16), season totals, per-game rates, last-3. Protected. |
| `Targets`, `Passing`, `Rushing` | New display tabs copying the existing pattern: B1 dropdown (data validation over `Teams!abbrev`), row 3 headers, A4 `=SORT(FILTER(...))` into the raw tab. Existing `2025 *` tabs untouched. |
| `PFF Ratings` | Keep; A1 note "PFF data unavailable (no subscription). Drop a CSV in data/manual/pff/ to populate." |
| `Config` | New: slate overrides (name, teams or kickoff window), garbage-time thresholds echo. Read by the pipeline. |
| `Log` | New, hidden: one row per run. |

Write mechanics: `values.batchUpdate` with `RAW`/`USER_ENTERED` per block, one
`spreadsheets.batchUpdate` for structure (add sheet, validation, protections,
frozen rows), ≤ 10 write requests per full run, exponential backoff on 429/5xx,
`--dry-run` prints a per-tab diff of changed cells.

## 6. Automation (approved 2026-09-09)

| Job | Cron (UTC) | Local (CT) | Runs/week | Est. minutes each | Weekly |
|-----|-----------|------------|-----------|-------------------|--------|
| Full rebuild | `0 11 * * *` | 06:00 daily | 7 | ~4 (uv cache + ~65 MB parquet) | 28 |
| Lines-only (`--only slates,vegas`) | `0 13,1 * * 2-6` | 08:00 and 20:00 Tue–Sat | 10 | ~1.5 | 15 |

≈ 43 min/week ≈ **190 min/month**, under 10% of the 2,000-minute Free plan
(Sep–Jan only; the workflow is a no-op outside the configured season window).
Lines-only path skips parquet downloads entirely (CFBD lines + DK lobby only).
Failures raise a step summary in the run log and the job goes red.

## 7. Phase checkpoints (unchanged from your brief)

2 Skeleton + auth → 3 Vegas/schedule/slates + main tab → 4 Pace + EPA → 5 RROE →
6 Player tabs → 7 Hardening → 8 Actions + first scheduled run on the test sheet.
Commit at each checkpoint, no push until you've reviewed.

## 8. Decisions I made (low-risk, flag if you disagree)

1. CFBD `/lines` (key confirmed working 2026-09-09; DraftKings + Bovada + openers)
   is the primary Vegas source with ESPN scoreboard (DraftKings) as fallback;
   CFBD `/games` + `/calendar` are primary for schedule and week, ESPN fallback.
2. ESPN week numbering is canonical (no week 0).
3. Main tab cells become values, not VLOOKUP formulas.
4. Per your 2026-09-09 note, the 2024/2025-labelled tabs and the weekly
   "Week N Targets" tabs in the dev sheet are deleted in Phase 3 (they were
   reference only); `DK `, `PFF Ratings`, and `Combined Ranks - Main` are kept.
5. Garbage-time thresholds 28/21/14 by quarter (configurable).
6. RROE rank 1 = most rush-heavy over expectation.
7. Scrambles counted as dropbacks via the "rusher is a passer in this game" rule.
