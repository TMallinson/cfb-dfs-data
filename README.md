# cfb-dfs-data

A scheduled data pipeline that keeps a Google Sheet current for DraftKings
college football DFS research. Every run pulls public sources, computes the
metrics below, and rewrites the sheet's tabs in place. It replaces a weekly
manual process built on sources that no longer exist.

* **Main tab** – one row per team per game on any DraftKings slate this week:
  slate, kickoff, Vegas total / spread / implied total, weather, pace, EPA
  splits, rush rate over expected, ranks, last-3 form.
* **Player tabs** – pick a team from a dropdown and see its pass catchers by
  targets, QBs by attempts, rushers by carries, with weekly columns.
* **Raw tabs** – every FBS team and every player with a touch, season-to-date
  and last-3, ranks with a visible denominator. Locked; the pipeline owns them.

Runs unattended on GitHub Actions (daily rebuild plus twice-daily line
refreshes) and locally with one command.

---

## Contents

1. [How it works](#how-it-works)
2. [Sheet layout](#sheet-layout)
3. [Data sources](#data-sources)
4. [Metric definitions](#metric-definitions)
5. [Setup from zero](#setup-from-zero)
6. [Running locally](#running-locally)
7. [Automation (GitHub Actions)](#automation-github-actions)
8. [Cutting over to the live sheet](#cutting-over-to-the-live-sheet)
9. [Configuration](#configuration)
10. [Repository layout](#repository-layout)
11. [Development](#development)
12. [Troubleshooting](#troubleshooting)

---

## How it works

```
sources/  (fetch + disk cache)      transform/  (pure functions, polars)     sheets/
  cfbd.py        ──► plays, drives ──► filters.py  garbage time, kneels, buckets
                     lines, games      pace.py     drive-based plays/min          layout.py  tab specs
                     teams, venues     epa.py      EPA per dropback / rush        writer.py  batched writes,
                     passing/rushing   rroe.py     rush-rate-over-expected model             dry-run diff,
                     plays, roster     players.py  targets / passing / rushing              protection,
  draftkings.py  ──► slates ─────────► slates.py   game ↔ slate assignment                  formatting
  espn.py        ──► schedule/odds ──► vegas.py    implied totals                 format.py  gradients
  openmeteo.py   ──► forecasts ──────► weather.py  kickoff-window summary         log.py     hidden Log tab
                                           │
                                     pipeline.py  stages: slates → vegas → weather → pace → epa → rroe → players
                                                  one stage failing never blocks the others
```

Each stage fetches, transforms, and produces a rectangular table. The sheet
is written once at the end with two batched requests plus a few structural
updates. A stage that fails is logged, its tab gets a `STALE:` banner and keeps
the previous rows, and the process exits non-zero so the Actions job goes red.
Stage-limited runs (the cheap lines-only path) reload untouched columns from
the raw tabs so the Main tab never loses data.

## Sheet layout

| Tab | Content | Refreshed by |
|-----|---------|--------------|
| `Main` | Team rows grouped by primary slate. A1 = last-updated stamp, status, which stages refreshed. Conditional formatting rebuilt every run. | every run |
| `Targets` / `Passing` / `Rushing` | Dropdown in B1 (native data validation over `Teams`); A4 holds a `FILTER`/`SORT` formula into the matching raw tab, so switching teams is instant and never needs a pipeline run. | formulas |
| `Config` | Manual slate definitions used when DraftKings detection fails or to override a DK slate with the same name. Either a team list or a weekday + kickoff window per row. | you |
| `Vegas Raw` | Every FBS-involved game of every week (rows accumulate through the season): book used, total, spread, ITT, openers, moneyline, slates, fetch time. | `vegas` |
| `Weather Raw` | Per game: venue, dome flag, temp at kickoff, precipitation chance/amount over the game window, wind, gusts, condition. | `weather` |
| `Pace Raw` / `EPA Raw` / `RROE Raw` | All FBS teams, season + last-3, ranks. Row 1 states the rank denominator, filters, and source. | `pace` / `epa` / `rroe` |
| `Targets Raw` / `Passing Raw` / `Rushing Raw` | Every player with a touch: Wk1–Wk16 volume, season totals, per-game rates, last-3, efficiency. | `players` |
| `Teams` | CFBD/ESPN team id, display abbreviation (DraftKings style), school, conference, classification. | every run |
| `Log` (hidden) | One row per run: timestamp, week, status, stages, rows per tab, sources, warnings, duration, CFBD calls, git SHA, runner. | every run |

Raw tabs and `Teams` are protected ranges editable by the service account and
the emails in `config.yaml`. A normal run never deletes a tab.

## Data sources

All verified with live calls; details, sample payloads, and fallbacks are in
[RESEARCH.md](RESEARCH.md).

| Need | Source | Notes |
|------|--------|-------|
| Lines, schedule, calendar, teams, venues | [CollegeFootballData](https://collegefootballdata.com) REST v2 (`api.collegefootballdata.com`, bearer key) | DraftKings book preferred, then Bovada, then consensus; ESPN's public scoreboard is the fallback for lines and schedule |
| Play-by-play, drives, PPA | CFBD `/plays`, `/drives` (one call per week each) | EPA = CFBD PPA. Chosen over the sportsdataverse ESPN parquet after that feed was found missing most plays for a third of week-1 games |
| Per-player passing / rushing | CFBD `/passing/plays`, `/rushing/plays`, `/roster` | 100% of target ids match the roster |
| DraftKings slates | DK lobby + draftables endpoints, unauthenticated | Snake and single-stat formats skipped (same games as classic) |
| Weather | CFBD `/venues` coordinates + [Open-Meteo](https://open-meteo.com) hourly forecasts (free, no key, 16 days) | CFBD's own weather endpoint is a paid tier |
| PFF grades, routes run | not available from any free source | replacements listed under Players below |

CFBD free tier is 1,000 calls per month. A full daily run costs about 5–10
calls, a lines-only run 1–2, and prior-season model training data is cached
permanently, so the schedule below uses roughly 300 calls per month.

## Metric definitions

Exact rules live in the module docstrings (`src/cfb_dfs/transform/*.py`) and
[PLAN.md](PLAN.md).

**Implied team total.** `total/2 − team_spread/2`, where `team_spread` is
from that team's perspective (negative = favored). Equivalent to favorite =
total/2 + |spread|/2, underdog = total/2 − |spread|/2. Source spreads (CFBD and
ESPN) are home-perspective, negative = home favored. The book used is written
on every row.

**Garbage time.** Score margin at the start of the play greater than 28 in
Q2, 21 in Q3, 14 in Q4 (never in Q1 or overtime). Kneels, spikes, and
penalties without a play are excluded everywhere. Thresholds are in
`config.yaml`.

**Pace.** Situation-neutral plays per minute of possession, computed at the
drive level because play-by-play clocks are stale in about half of all games:
scrimmage plays on qualifying drives ÷ drive elapsed minutes. A drive
qualifies if it does not start in garbage time, does not start inside the final
2:00 of a half, and is not an end-of-half kneel-out. Seconds per play = 60 ÷
plays per minute. Rank 1 = fastest. The raw tab also carries the
all-situations number.

**EPA.** CFBD PPA per play. *Dropback* = pass attempts, sacks, interceptions.
*Rush* = designed rushes. No feed flags scrambles, so QB rushes count as rushes
(`include_scrambles_in_dropback` exists but is off). Offense value = mean EPA
on the team's offensive plays in the bucket; defense value = mean EPA allowed.
Offense rank 1 = highest, defense rank 1 = lowest allowed. Our values
correlate 0.94–0.95 with CFBD's own season PPA aggregates, which are shown
alongside in `EPA Raw`.

**RROE% (rush rate over expected).** A gradient-boosted classifier predicts
P(rush) from down, distance, yards to goal, score margin, seconds left in the
half, half, period, offense timeouts, pregame spread (offense perspective) and
total. Trained on the prior season plus the current one (FBS offenses, garbage
time excluded) with a 20% holdout grouped by game; the quality report with
log loss, Brier score, and a calibration table is regenerated each run at
[reports/rroe_model_report.md](reports/rroe_model_report.md). Team RROE% =
mean(actual rush − P(rush)) × 100 on 1st and 2nd down. Rank 1 = most
run-heavy over expectation.

**Splits.** Every team and player metric has season-to-date and last-3 team
games. Both are in the raw tabs; Main shows season with last-3 as secondary
columns.

**Ranks.** FBS teams only, ties share the better rank, denominator printed in
row 1 of each raw tab. FCS opponents on a slate show values but `FCS` in rank
cells.

**Players.** Targets = pass attempts with a named receiver (throwaways,
spikes, and unparsed attempts excluded). Dropbacks = attempts + sacks taken.
Carries = individually attributed rushes only (no sacks, kneels, team rushes).
Per-game rates use games with at least one touch; `Team games` is shown for
context. Routes run are not published anywhere free; `Tgt/team DB` (targets
per team dropback in the player's games) is the closest proxy to targets per
route. Other replacements for PFF-only columns: target share, aDOT, air yards,
YAC, red-zone targets, PPA per touch, success rate, designed runs.

**Weather.** Temperature at the kickoff hour; maximum precipitation chance
and total precipitation over kickoff + 3 hours; mean wind and max gust over the
same window; condition text from the WMO code. Domes show `Dome`.

**Conditional formatting.** Relative columns anchor colors at the 10th / 50th
/ 90th percentiles so outliers don't dominate. Weather uses fixed thresholds:
temperature blue ≤ 35°F to red ≥ 95°F; wind white through 12 mph, orange at 15,
red at 20+ (published NFL studies show completion percentage falling from ~60%
under 10 mph to ~55% at 20+ mph); precipitation chance white to 20%, orange 50%,
red 80%+. Rank columns are green at 1, except RROE's rank which is flipped so
pass-heavy teams show green.

## Setup from zero

1. **Tooling.** Python 3.11+ and [uv](https://docs.astral.sh/uv/):
   `curl -LsSf https://astral.sh/uv/install.sh | sh`, then `uv sync` in the
   repo root.
2. **CollegeFootballData key.** <https://collegefootballdata.com/key> (free
   tier is enough).
3. **Google service account.** In Google Cloud Console: create or pick a
   project → *APIs & Services* → enable **Google Sheets API** → *Credentials* →
   create a **service account** → *Keys* → add a JSON key and download it.
   Store it outside the repo, e.g. `~/.config/cfb-dfs/service-account.json`.
4. **Share the sheet.** Open the Google Sheet → *Share* → add the service
   account's email (`...@...iam.gserviceaccount.com`) as **Editor**.
5. **Local secrets.** Copy `.env.example` to `.env` and fill in
   `CFBD_API_KEY`, `SHEET_ID` (the long id in the sheet URL), and
   `GOOGLE_APPLICATION_CREDENTIALS` (path to the JSON key). `.env` is
   gitignored; never commit it.
6. **GitHub secrets** (repo → *Settings* → *Secrets and variables* → *Actions*):
   `CFBD_API_KEY`, `SHEET_ID`, and `GOOGLE_SERVICE_ACCOUNT_B64` =
   `base64 < service-account.json` (single line).
7. **Verify.** `uv run python -m cfb_dfs smoke` prints the CFBD tier and
   remaining calls plus the sheet title and tab count.
8. **First run on a copy of your sheet.** `uv run python -m cfb_dfs run --dry-run`
   shows what would change; `uv run python -m cfb_dfs run` writes it. The
   pipeline creates any missing tabs. If the sheet has old tabs you want gone,
   `uv run python -m cfb_dfs reset-sheet` lists them and `--yes` deletes them
   (tabs with owner-only protection must be unprotected by the owner first).

## Running locally

```
uv run python -m cfb_dfs run                            # all stages, auto-detected week
uv run python -m cfb_dfs run --week 5                   # pin the week
uv run python -m cfb_dfs run --only slates,vegas,weather   # cheap lines-only refresh
uv run python -m cfb_dfs run --dry-run                  # fetch + compute, print per-tab diff, write nothing
uv run python -m cfb_dfs run --refresh                  # bypass the disk cache
uv run python -m cfb_dfs run --sheet-id <id>            # override SHEET_ID
uv run python -m cfb_dfs run --force                    # run outside the season window
uv run python -m cfb_dfs week                           # show the detected week
uv run python -m cfb_dfs smoke                          # live credential checks
uv run python -m cfb_dfs inspect-sheet                  # read-only structure dump to reports/
uv run python -m cfb_dfs reset-sheet [--yes]            # one-time tab cleanup
```

Stages: `slates`, `vegas`, `weather`, `pace`, `epa`, `rroe`, `players`.
`slates` always runs. Week detection uses the CFBD calendar (weeks roll over
Monday 02:00 CT); override with `--week` or `week:` in `config.yaml`.

Raw API responses are cached under `data/cache/` (gitignored): lines 1 hour,
games and PPA 6 hours, the current week's plays 6 hours, completed weeks 14
days, prior seasons forever. `--refresh` bypasses reads but still writes.

## Automation (GitHub Actions)

[`.github/workflows/pipeline.yml`](.github/workflows/pipeline.yml):

| Trigger | UTC | Central (CDT / CST) | Stages |
|---------|-----|---------------------|--------|
| daily | `0 11 * * *` | 06:00 / 05:00 | all |
| Tue–Sat morning | `0 13 * * 2-6` | 08:00 / 07:00 | `slates,vegas,weather` |
| Tue–Sat evening | `0 1 * * 0,3-6` | 20:00 / 19:00 | `slates,vegas,weather` |
| manual (*Actions → Run workflow*) | — | — | inputs: stages, week, dry run |

About 190 Actions minutes per month, under 10% of the Free plan. Each job
restores the data cache, installs with uv, smoke-tests both credentials, runs
the pipeline, writes a stage/rows/warnings table to the job summary, and
uploads `reports/` as an artifact. Runs outside the `season_window` in
`config.yaml` exit as no-ops. Overlapping runs are serialized.

Trigger and watch from the CLI:

```
gh workflow run "cfb-dfs pipeline"
gh run watch
```

## Cutting over to the live sheet

Develop and validate against a copy first (the default). To switch:

1. Share the live sheet with the service account as **Editor**.
2. Optionally remove old tabs: unprotect any owner-only ranges, then
   `uv run python -m cfb_dfs reset-sheet --sheet-id <live id> --yes`.
3. Point `SHEET_ID` at the live id in `.env` and in the repository secret.
4. `uv run python -m cfb_dfs run --dry-run` then `run`, or trigger the
   workflow.

Everything else (tabs, formulas, protections, formatting) is created on the
first run.

## Configuration

`config.yaml` (non-secret, committed):

| Key | Purpose |
|-----|---------|
| `season`, `week`, `season_type`, `timezone` | week auto-detects when `week` is null |
| `season_window` | first/last dates the scheduled job does work |
| `cache.ttl_hours` | per-endpoint cache lifetimes |
| `sources.*.enabled` | feature flags per provider; `cfbd.line_provider_preference` sets book order |
| `slates.kickoff_windows_ct` | fallback slate labels when neither DraftKings nor the Config tab define slates |
| `metrics.garbage_time`, `two_minute_filter`, `include_scrambles_in_dropback`, `recent_games_window` | metric rules |
| `metrics.rroe` | training seasons, holdout fraction, early downs, low-sample threshold |
| `sheet.tabs`, `sheet.protected_editor_emails` | tab names and who can edit raw tabs |

`data/overrides/team_aliases.yaml` maps DraftKings names/abbreviations to
CFBD schools when automatic matching misses (the run log names any miss).

Secrets are read from environment variables only: `CFBD_API_KEY`, `SHEET_ID`,
and either `GOOGLE_APPLICATION_CREDENTIALS` (path) or
`GOOGLE_SERVICE_ACCOUNT_B64` (base64 JSON). Nothing secret is ever logged.

## Repository layout

```
config.yaml                  settings (see above)
src/cfb_dfs/
  cli.py                     typer commands
  pipeline.py                stage orchestration, table builders, run summary
  config.py  week.py         settings / secrets, week detection
  models/                    pydantic schemas for source records
  sources/                   cfbd, draftkings, espn, openmeteo, sportsdataverse, cache, http
  transform/                 filters, plays, pace, epa, rroe, players, slates, vegas, weather, teams, ranks, splits
  sheets/                    auth, client (retries), writer, layout, format, log, inspect
tests/                       unit + end-to-end tests on real fixture slices (no network)
data/cache/                  gitignored API cache
data/overrides/              team alias overrides
reports/                     rroe_model_report.md, sheet_structure.md, run_summary.md
.github/workflows/           pipeline.yml
RESEARCH.md  PLAN.md         source verification and design decisions
```

## Development

```
uv run pytest                      # 55 tests, fixtures only
uv run ruff check . && uv run ruff format .
uv run mypy
```

Conventions: sources never compute metrics; transforms are pure functions
over polars frames; the sheet layer only knows rectangular value blocks. Add a
new metric by writing a transform with a fixture test, a `TabSpec` in
`sheets/layout.py`, a table builder in `pipeline.py`, and (optionally) a
`ColorRule` in `sheets/format.py`.

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `smoke` reports CFBD 401 | Key rejected or expired: regenerate at collegefootballdata.com/key, update `.env` and the `CFBD_API_KEY` secret. Lines and schedule fall back to ESPN's scoreboard; pace, EPA, RROE, and players need CFBD. |
| `CFBD budget low` warning | Under 100 calls left this month; reduce cron frequency or wait for the reset date shown by `smoke`. |
| `DraftKings slates unavailable` | DK lobby blocked or empty. Fill the `Config` tab; otherwise the kickoff-window heuristic labels games and A1 says so. |
| `teams.unmatched_dk_team` in the log | Add the DK name or abbreviation to `data/overrides/team_aliases.yaml`. |
| `Open-Meteo request failed` | Weather blank for that run; the previous forecast stays on Weather Raw. |
| `STALE:` banner on a raw tab | That stage failed; rows are from the last good run. The Log tab and the Actions summary carry the error. |
| Player tab shows `No rows for X` | Pick an abbreviation from the dropdown; the list comes from `Teams`. |
| `You can't remove yourself as an editor` | Protected-range editors must include the service account; the pipeline adds it automatically. |
| Sheets 429 / 5xx | Retried with exponential backoff (6 attempts); a full run makes about 15 requests. |
| Wrong week | `--week N` or `week:` in `config.yaml`. |
| Workflow skipped everything | Today is outside `season_window`; adjust the dates or run manually with `--force` locally. |
