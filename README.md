# cfb-dfs-data

Scheduled pipeline that fills a Google Sheet used for DraftKings college football
DFS research: Vegas lines and implied totals per DK slate, game weather, pace,
EPA splits, rush rate over expected, and per-player target / passing / rushing
volume with dropdown-driven display tabs.

Sources and every metric definition are documented in [RESEARCH.md](RESEARCH.md)
and [PLAN.md](PLAN.md); the RROE model's quality report is regenerated at
[reports/rroe_model_report.md](reports/rroe_model_report.md).

## Sheet layout

| Tab | Content | Written by |
|-----|---------|------------|
| `Main` | One row per team per game on any DraftKings slate this week: slate, kickoff (CT), total, spread, implied total, weather, pace + rank, offense/defense EPA per dropback and per rush + ranks, RROE% + rank, last-3 columns. A1 = last-updated / status. Conditional formatting is rebuilt every run. | every run |
| `Vegas Raw` | Every FBS-involved game of every week (rows accumulate through the season): book, total, spread, ITT, openers, moneyline, slates. | `vegas` |
| `Weather Raw` | Per game: venue, dome, temp at kickoff, precip chance/amount, wind, gusts, condition. | `weather` |
| `Pace Raw`, `EPA Raw`, `RROE Raw` | All 138 FBS teams, season + last-3, ranks (denominator and filter definitions in row 1). | `pace`, `epa`, `rroe` |
| `Targets Raw`, `Passing Raw`, `Rushing Raw` | Every player with a touch: weekly columns Wk1..Wk16, season totals, per-game rates, last-3. | `players` |
| `Targets`, `Passing`, `Rushing` | Pick a team in B1; `FILTER`/`SORT` formulas pull that team's rows live from the raw tab. | formulas (set up by `players`) |
| `Teams` | Team ids, display abbreviations, school, conference, classification. | every run |
| `Config` | Manual slate overrides used when DraftKings detection fails. | you |
| `Log` (hidden) | One row per run: status, rows per tab, sources, warnings, duration, CFBD calls. | every run |

Raw tabs are protected (pipeline + the emails in `config.yaml` can edit). Nothing
is ever deleted by a normal run; tabs are cleared and rewritten by range.

## Setup from zero

1. **Python and uv.** Python 3.11+; install [uv](https://docs.astral.sh/uv/)
   (`curl -LsSf https://astral.sh/uv/install.sh | sh`), then `uv sync`.
2. **CollegeFootballData key.** Request one at <https://collegefootballdata.com/key>
   (free tier: 1,000 calls/month; a full daily run uses about 5, a lines-only run 1,
   prior-season model data is cached permanently).
3. **Google service account.** In Google Cloud: create a project, enable the
   *Google Sheets API*, create a service account, download its JSON key. Locally,
   store it outside the repo (e.g. `~/.config/cfb-dfs/service-account.json`).
4. **Share the sheet** with the service account's email as *Editor*.
5. **`.env`** in the repo root (gitignored; see `.env.example`):
   `CFBD_API_KEY`, `SHEET_ID` (from the sheet URL), `GOOGLE_APPLICATION_CREDENTIALS`
   (path to the JSON key). In CI use `GOOGLE_SERVICE_ACCOUNT_B64` = `base64 < key.json`
   instead of the path.
6. **Check everything:** `uv run python -m cfb_dfs smoke` (CFBD tier + remaining
   calls, sheet title + tab count).
7. **First run on a copy of your sheet:** `uv run python -m cfb_dfs run --dry-run`,
   then `uv run python -m cfb_dfs run`.

`config.yaml` holds every non-secret setting: season, week override, timezone,
season window, cache TTLs, source feature flags, garbage-time thresholds, RROE
training seasons, tab names, protected-range editors.

## Running

```
uv run python -m cfb_dfs run                       # everything for the auto-detected week
uv run python -m cfb_dfs run --week 5              # pin the week
uv run python -m cfb_dfs run --only slates,vegas,weather   # the cheap lines-only path
uv run python -m cfb_dfs run --dry-run             # fetch + compute, print a per-tab diff, write nothing
uv run python -m cfb_dfs run --refresh             # bypass the disk cache (data/cache, gitignored)
uv run python -m cfb_dfs run --sheet-id <id>       # override SHEET_ID
uv run python -m cfb_dfs week | smoke | inspect-sheet | reset-sheet
```

Stages: `slates`, `vegas`, `weather`, `pace`, `epa`, `rroe`, `players`. A
stage-limited run keeps the other Main columns populated from the raw tabs.
Exit code 1 means at least one stage failed (its tab gets a `STALE:` banner in
A1 and keeps the last good rows); the Log tab and the Actions summary say which.

Development: `uv run pytest`, `uv run ruff check . && uv run ruff format .`,
`uv run mypy`.

## Metric definitions (short form)

* **Implied total** = total/2 − team spread/2 with spread from the team's
  perspective (negative = favored). DraftKings line preferred, else Bovada, else
  consensus; the book used is written per row.
* **Garbage time** = margin > 28 in Q2, 21 in Q3, 14 in Q4 (never in Q1/OT).
  Kneels, spikes and no-play penalties are always excluded.
* **Pace** = scrimmage plays / drive minutes over qualifying drives (not garbage
  time at drive start, not starting inside the final 2:00 of a half, not an
  end-of-half kneel-out). Rank 1 = fastest.
* **EPA** = CFBD PPA per play. Dropback = pass attempts + sacks + interceptions;
  rush = designed rushes (QB rushes count as rushes; no feed flags scrambles).
  Offense rank 1 = highest; defense rank 1 = lowest allowed.
* **RROE%** = (actual rushes − model-expected rushes) / plays × 100 on 1st/2nd
  down, garbage time excluded. Rank 1 = most run-heavy over expectation.
* **Ranks** cover FBS teams only, ties share the better rank, the denominator
  is printed in each raw tab's row 1. FCS opponents on a slate show values but
  `FCS` in rank cells.
* **Players**: targets = attempts with a named receiver; dropbacks = attempts +
  sacks; carries = individually attributed rushes only. Routes run are not
  published by any free source; `Tgt/team DB` (targets per team dropback in the
  player's games) is the closest proxy.

## Automation

`.github/workflows/pipeline.yml` runs a full rebuild daily at 06:00 CT and a
lines-only refresh at 08:00 and 20:00 CT Tuesday–Saturday during the season
window, using the repository secrets `CFBD_API_KEY`, `SHEET_ID`,
`GOOGLE_SERVICE_ACCOUNT_B64`. Roughly 190 Actions minutes per month. Point
`SHEET_ID` at the live sheet only when you are ready to cut over.

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `smoke` says CFBD 401 | Key rejected: regenerate at collegefootballdata.com/key, update `.env` and the GitHub secret. Lines/schedule fall back to ESPN's public scoreboard; pace/EPA/RROE/players need CFBD. |
| `CFBD budget low` warning | Fewer than 100 calls left this month. Lower the cron frequency or wait for the reset date shown by `smoke`. |
| `DraftKings slates unavailable` | DK lobby blocked or empty. Fill the `Config` tab (slate name + team abbreviations, or weekday + kickoff window); otherwise the kickoff-window heuristic labels games. |
| Main tab shows `FCS` in rank cells | Expected for FCS opponents. |
| `Open-Meteo request failed` | Weather columns blank for the run; the previous forecast stays in Weather Raw. |
| `STALE:` banner on a raw tab | That stage failed; rows are from the last good run. Check the Log tab / Actions log for the error. |
| `teams.unmatched_dk_team` warning | Add the DK name or abbreviation to `data/overrides/team_aliases.yaml`. |
| Sheets 429 / 5xx | Retried with exponential backoff (6 attempts). A full run makes ~15 requests. |
| Wrong week detected | `--week N`, or set `week:` in `config.yaml`. Weeks follow the CFBD calendar (Monday 02:00 CT rollover). |
