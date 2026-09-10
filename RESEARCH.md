# RESEARCH.md — data source verification

Verified 2026-09-09 (US/Central) with live HTTP requests from this machine. Every
"confirmed working" row below was actually called and the response inspected;
sample snippets are trimmed copies of real responses. Nothing here was taken from
docs alone unless marked as such.

## Summary table

| # | Data need | Chosen source | Confirmed today | Fallback |
|---|-----------|---------------|-----------------|----------|
| 1 | Vegas total / spread (DraftKings book) | ESPN public scoreboard (`site.api.espn.com`), odds block, provider = DraftKings | **Yes** (85 of 86 week-2 FBS games had DK lines, no auth) | CFBD `/lines` (needs working key; per-book incl. DraftKings), then sportsdataverse `espn_cfb_betting` parquet (resolved lines only, post-game) |
| 2 | Weekly schedule / game list | ESPN public scoreboard (same call as #1) | **Yes** (86 week-2 events, ESPN team ids) | CFBD `/games`; DraftKings draftables competitions |
| 3 | DraftKings slate membership | DraftKings lobby + draftables endpoints | **Yes** (14 CFB draft groups, unauthenticated, works even without a browser User-Agent) | Manual `Config` tab / YAML slate definitions; kickoff-window heuristic |
| 4 | Play-by-play with EPA, passer/rusher/receiver names, clock, score | sportsdataverse `espn_cfb_pbp` parquet (GitHub release asset, rebuilt daily by the cfbfastR-cfb-data python pipeline) | **Yes** (2026: 11,795 plays / 97 games; 2025: 166,053 plays / 956 games / weeks 1–16) | Legacy `cfbfastR_cfb_pbp` parquet (same host, more games incl. FCS-only, worse player names); CFBD `/plays` (key) |
| 5 | Pace (plays per minute of possession) | Computed from #4 (play clocks) and `espn_cfb_drives` parquet (`time_elapsed`, `offensive_plays`) | **Yes** (1,252 of 1,253 drives have elapsed time) | CFBD `/drives` (has `elapsed`, key) |
| 6 | EPA/dropback, EPA/rush, off + def | Computed from #4 (`EPA`, `pass`, `rush`, `sack`, `kneel_down`, `pos_score_diff_start`) | **Yes** (EPA null rate 0.0% in 2026 file) | CFBD `/ppa/teams`, `/ppa/games` (key) as cross-check only |
| 7 | RROE% model | Trained on #4 (2025 + 2026) | **Yes** (features present: down, distance, yards to goal, score diff, clock, half, timeouts) | none needed |
| 8 | Targets / passing / rushing per player | Computed from #4; validated against `espn_cfb_adv_receiving` / `adv_passing` / `adv_rushing` parquet (same pipeline, per game) | **Yes** (95.4% of pass attempts carry a receiver name; sacks separated) | CFBD `/passing/plays` (has `targetId`), `/rushing/plays` (key) |
| 9 | Team metadata (FBS flag, conference, abbreviations, logos, ESPN↔CFBD name) | sportsdataverse `espn_cfb_teams` parquet (`is_fbs`, `classification`, `cfbd_conference`, `school`) | **Yes** (827 teams, 138 classified `fbs`) | CFBD `/teams/fbs` (key) |
| 10 | Season calendar / week detection | ESPN scoreboard `week.number` + `season.year`; config override | **Yes** | CFBD `/calendar` (key) |
| 11 | CollegeFootballData API (general) | `https://api.collegefootballdata.com` (v2; `apinext` host is an alias) | **Endpoints reachable, but your key returns 401** — see §CFBD | — |
| 12 | PFF grades | No free API. Placeholder tab + optional local CSV ingest | n/a | manual CSV drop in `data/manual/pff/` |
| 13 | Google Sheets | Sheets API v4 via service account | **Yes** (read test sheet: 50 tabs, protections, validations, formulas) | — |

## 1. Vegas lines — ESPN public scoreboard (primary)

**Endpoint** (GET, no auth, no key):

```
https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard
  ?dates=2026&seasontype=2&week=2&groups=80&limit=400
```

`groups=80` = FBS. Result today: HTTP 200, 86 events, 85 with an `odds` block, sole
provider `DraftKings`.

Sample (trimmed):

```json
{"week":{"number":2},"season":{"type":2,"year":2026},
 "events":[{"id":"401858213","date":"2026-09-11T00:00Z",
   "competitions":[{"competitors":[
      {"homeAway":"home","team":{"id":"2390","abbreviation":"MIA","displayName":"Miami Hurricanes"}},
      {"homeAway":"away","team":{"id":"50","abbreviation":"FAMU","displayName":"Florida A&M Rattlers"}}],
     "odds":[{"provider":{"name":"DraftKings"},"details":"MIA -57.5","overUnder":63.5,
              "spread":-57.5,"homeTeamOdds":{"favorite":true}}]}]}]}
```

Per-event, per-book odds are also exposed at
`https://sports.core.api.espn.com/v2/sports/football/leagues/college-football/events/{id}/competitions/{id}/odds`
(confirmed, 1 item = DraftKings). Not needed unless ESPN adds more books.

**Spread sign convention (adopted everywhere):** ESPN `spread` is from the home
team's perspective, negative = home favored (`MIA -57.5` → `spread: -57.5`). The
pipeline stores a per-team `spread` where **negative = that team is favored**, so
`implied_total = total/2 − team_spread/2`. This equals the required
`favorite = total/2 + |spread|/2`, `underdog = total/2 − |spread|/2`.

**Caveats:** ESPN carries one book (DraftKings) for CFB today; if a game has no
odds block the row is written with blank total/spread and a "no line" note. Line
provider is written per row (`line_source = "DraftKings via ESPN"`).

**Fallback:** CFBD `/lines?year=2026&week=2` returns `lines[]` per game with
`provider` (DraftKings, Bovada, ESPN Bet, consensus), `spread`, `overUnder`,
`spreadOpen`, `overUnderOpen`. Schema confirmed from the live OpenAPI spec; the
call itself could not be executed because the key is rejected (see §CFBD).

## 2. Schedule — ESPN scoreboard (primary)

Same call as §1. Gives event id, kickoff (UTC), home/away ESPN team ids and
abbreviations, week number, status. ESPN team ids are the same ids used in the
play-by-play and teams parquet files, so joins are by integer id, not name.

**Week numbering note:** ESPN has no "week 0". Games from Aug 29 – Sep 7 are all
ESPN week 1; the upcoming Sep 10–12 slate is ESPN week 2. The pipeline uses ESPN
week numbering as canonical (it is what the play-by-play feed uses) and lets
`--week` override. If CFBD calendar numbering differs once your key works, the
pipeline maps CFBD weeks to ESPN weeks by kickoff date.

## 3. DraftKings slates (primary, unauthenticated)

**Lobby:** `GET https://www.draftkings.com/lobby/getcontests?sport=CFB` → HTTP 200,
1.36 MB JSON. Works with the default curl User-Agent as well as a browser UA. No
login, no cookies. `DraftGroups[]` is the slate list:

```json
{"DraftGroupId":153231,"ContestTypeId":94,"StartDateEst":"2026-09-12T12:00:00.0000000",
 "StartDate":"2026-09-12T16:00:00.0000000Z","Sport":"CFB","GameCount":12,
 "ContestStartTimeSuffix":"","DraftGroupTag":"Featured","GameSetKey":"0ED8..."}
```

`GameTypes` seen today: 94 = Classic, 95 = Showdown Captain Mode, 377 = Snake.
`ContestStartTimeSuffix` is DK's own slate label (`""` = main, `" (Afternoon)"`,
`" (Night)"`, `" (Late Night)"`, `" (OU @ MICH)"` for showdowns).

**Draftables (games in a slate):**
`GET https://api.draftkings.com/draftgroups/v1/draftgroups/{DraftGroupId}/draftables?format=json`
→ HTTP 200, `competitions[]`:

```json
{"competitionId":6182076,"name":"PSU @ TEMP","startTime":"2026-09-12T16:00:00.0000000Z",
 "homeTeam":{"teamId":3421,"teamName":"Owls","abbreviation":"TEMP","city":"Temple"},
 "awayTeam":{"teamId":3440,"teamName":"Nittany Lions","abbreviation":"PSU","city":"Penn State"},
 "sport":"CFB","competitionState":"Upcoming"}
```

All 14 CFB draft groups for the week were fetched successfully (12-game main,
2-game Friday, 6 afternoon, 7 night, 4 late night, 8 showdowns, 1 snake).

**Team matching:** DK abbreviations differ from ESPN for 8 of 52 teams seen
(`BAMA`, `RU`, `MIZZ`, `IL`, `TEMP`, `AF`, `NMST`, `ULL`). DK does give `city` +
`teamName` ("Penn State" + "Nittany Lions") which matches ESPN `displayName`
("Penn State Nittany Lions"), so matching is by normalized full name with a
small manual override file for the rest. DK abbreviations match 48 of 52 of the
ABBREV keys already used in your sheet, so the main tab will keep using
DK-style abbreviations as its display key.

**Fallback:** if DK ever blocks these calls, a `Config` tab (slate name + team
abbreviations or kickoff window) drives the filter, and a kickoff-window
heuristic (Sat 11:00 CT main, 14:30 afternoon, 18:00 night, 21:00 late; Tue–Fri
single-window slates) labels games when nothing else is available. The main
tab always renders and always carries a `slate` and `slate_source` column.

## 4. Play-by-play — sportsdataverse `espn_cfb_pbp` (primary)

**Where:** GitHub release assets on `sportsdataverse/sportsdataverse-data`,
tag `espn_cfb_pbp`, file `play_by_play_{season}.parquet`. Direct URL:

```
https://github.com/sportsdataverse/sportsdataverse-data/releases/download/espn_cfb_pbp/play_by_play_2026.parquet
```

Produced by `sportsdataverse/cfbfastR-cfb-data` (Python successor to the R
pipeline; last push 2026-09-09 15:20 UTC). Data dictionary: that repo's
`DATASETS.md` (19 datasets, ~928 columns). The old `cfbfastR-data` repo's
in-tree parquet stops at 2021 — do not use it. There are no GitHub releases on
`cfbfastR-data` itself.

**Freshness observed:** 2026 asset updated 2026-09-09 15:11 UTC (same-day
after Monday-night game). 2025 asset updated 2026-09-07.

**Coverage:** 2026 file = 97 games, all ESPN week 1 (Aug 29 – Sep 7). Of 99
FBS-involved games in the ESPN schedule, 97 are present; the 2 missing
(Duke–Tulane, Charlotte–Citadel) are `STATUS_DELAYED` in ESPN's schedule. The
pipeline reports games-missing-from-pbp per run.

**Key columns (498 total):** `game_id, season, week, half, period, clock.minutes,
clock.seconds, pos_team_id, pos_team, def_pos_team_id, down, distance,
start.yardsToEndzone, pos_score_diff_start, rush, pass, pass_attempt, sack,
kneel_down, penalty_no_play, scrimmage_play, type.text, text, EPA, def_EPA,
passer_player_name/id, rusher_player_name/id, receiver_player_name/id, target,
drive.id, drive.timeElapsed.displayValue, start.posTeamTimeouts`.

Sample rows (USC vs San José State):

```
period clock  half pos_team  down dist ytg  rush  pass  EPA    receiver      passer        rusher
1      14:47  1    USC       1    10   75   true  false 0.83   null          null          Waymond Jordan
1      14:15  1    USC       1    10   50   false true  1.21   Mark Bowman   Jayden Maiava null
```

**Flags verified:** `kneel_down` is populated (48 true). Spikes are not flagged
but are unambiguous in `text` ("pass incomplete, Spike") — 3 in week 1. There
is no explicit scramble flag; scrambles are recorded as `rush` with the QB as
rusher and need a position-based rule (see PLAN.md, EPA buckets).

**Comparison with the legacy `cfbfastR_cfb_pbp` release** (same host, tag
`cfbfastR_cfb_pbp`, R pipeline, updated 2026-09-08):

| | `espn_cfb_pbp` (chosen) | `cfbfastR_cfb_pbp` (legacy) |
|---|---|---|
| 2026 games | 97 (FBS-involved) | 203 (adds FCS-vs-FCS) |
| Player names | Full ("KJ Duff") | Often jersey-abbreviated ("#8 S.Gbatu") |
| Receiver on pass attempts | 95.4% | 89.5% |
| EPA null rate | 0.0% | 3.7% |
| Columns | 498 | 362 |
| Maintainer status | active successor | "legacy, still scheduled" per README |

**Comparison with parsing CFBD `/plays` text ourselves:** CFBD `/plays` returns
`playText`, `ppa`, `playType`, clock, score but no receiver/passer/rusher
fields, so targets would require regex on text. CFBD's newer `/passing/plays`
endpoint *does* carry `passerId`, `targetId`, `target`, `isSpike`,
`isThrowaway` — a cleaner option than text parsing — but it needs a working key
and counts against a 1,000-call/month free tier. **Decision: use the
sportsdataverse parquet as primary for all play-level metrics; use CFBD
`/passing/plays` only as an optional cross-check when the key works.**

## 5. Pace

Two inputs, both confirmed:

- `espn_cfb_drives` parquet (`drives_2026.parquet`): `team_id, game_id,
  offensive_plays, n_plays, time_elapsed ("6:05"), start_clock, end_clock,
  start_period, end_period, result`. 1,252 of 1,253 drives have `time_elapsed`.
  A quick all-situations calc gave Stanford 2.92 plays/min, Ole Miss 2.80, LSU
  2.72 — plausible versus the 2.6–2.8 range in your existing Pace tab.
- Play-level clocks from §4 for the situation-neutral number (garbage time,
  kneels, spikes, final 2:00 of each half removed).

## 6. EPA

`EPA` on every scrimmage play in §4, from the cfbfastR EP model. Team splits are
computed by the pipeline (bucket definitions in PLAN.md). CFBD `/ppa/teams` and
`/ppa/games` (`offense.passing`, `offense.rushing`, `excludeGarbageTime` param)
exist per the OpenAPI spec and will be wired as a cross-check column once the
key works, but they don't provide last-3-game windows or our exact bucket rules.

## 7. RROE%

All model features are present in §4: `down, distance, start.yardsToEndzone,
pos_score_diff_start, period, half, clock.minutes/seconds,
start.posTeamTimeouts`. Training data: 2025 (956 games) + 2026 to date.

## 8. Player volume (targets / attempts / carries)

From §4: `receiver_player_name` on `pass_attempt` rows; `rusher_player_name` on
`rush` rows; `passer_player_name` on `pass_attempt` rows. Positions come from
`espn_cfb_rosters` / `game_rosters` release assets (same host; used only for
the position column).

Cross-check datasets (same pipeline, per player per game, confirmed):
`espn_cfb_adv_receiving` (`Tar, Rec, Yds, aDOT, AirYds, YAC, EPA`),
`espn_cfb_adv_passing` (`Att, Comp, Yds, Sck, CPOE, aDOT`),
`espn_cfb_adv_rushing` (`Car, Yds, EPA, SR`). Rutgers week 1 from pbp: KJ Duff
16 targets, Jourdin Houston 7, Dyzier Carter 4 — consistent with `adv_receiving`.

## 9. Team metadata

`espn_cfb_teams` release (`cfb_teams_2026.parquet`, 827 rows, 54 cols):
`team_id, abbreviation, display_name, school, location, name, is_fbs,
classification (fbs/fcs/ii/iii), conference_short_name, cfbd_conference,
team_logo, alt_name1..3`. 138 teams classified `fbs` (148 flagged `is_fbs`;
the pipeline uses `classification == "fbs"` for the rank denominator and
reports the count in the sheet). `school` matches CFBD naming, which gives a
free ESPN↔CFBD crosswalk.

## 11. CollegeFootballData API

- **Host:** `https://api.collegefootballdata.com` is canonical (v2, OpenAPI
  5.27.1 fetched live from `/api-docs.json`, 84 endpoints).
  `apinext.collegefootballdata.com` still resolves and behaves identically; v1
  was shut down before the 2025 season.
- **Auth:** `Authorization: Bearer <key>` (confirmed from docs and from the
  401 body `{"message":"Unauthorized"}` when the header is wrong or missing).
- **Tiers (from collegefootballdata.com/api-tiers):** Free = 1,000 calls/month,
  no live play-by-play, no GraphQL. Tier 1 ($1) 5k, Tier 2 ($5) 30k + live
  pbp, Tier 3+ ($10–30) 75k–500k + GraphQL. No per-minute rate limit is
  published. `/info` reports remaining calls for the authenticated key.
- **Data availability (docs):** lines 2013–present, plays 2001–present, PPA
  2001–present, player season stats 2004–present; "current-season values can
  change as games are played".
- **Your key: rejected.** Every call with `CFBD_API_KEY` from `.env` returned
  HTTP 401 on both hosts, including `/info`, `/calendar?year=2026`, and
  `/teams/fbs`. The value is 40 lowercase hex characters with no
  whitespace, quotes, or CR. I could not find a documented key format to
  compare against, and the v2 announcement says nothing about key
  invalidation. **Action for you:** request/regenerate a key at
  https://collegefootballdata.com/key and replace `CFBD_API_KEY` in `.env` and
  the GitHub secret. Until then the pipeline runs fully on ESPN +
  sportsdataverse + DraftKings; CFBD is wired as a fallback and cross-check
  behind a feature flag, and its client has a live smoke test in Phase 2.
- **Budget when it works:** ~6 calls per full run + 1 per lines-only run ≈
  350 calls/month on the proposed schedule, inside the free tier.
- **SDK:** not used. The official `cfbd` Python package exists but the thin
  typed client (httpx + pydantic) covers the 6–8 endpoints we need and matches
  the live spec exactly.

## 12. PFF

No public API; PFF Premium Stats exports are the only route. The `PFF Ratings`
tab is kept, cleared of stale data, and gets an explanatory note. If a CSV is
dropped into `data/manual/pff/team_grades.csv` (header-mapped, documented in
README) the pipeline writes it to a `PFF Raw` tab. Player-level PFF columns
that exist in your current Targets/Passing/Rushing tabs (grades, routes, YPRR,
slot %, TTT, BTT%, TWP%) **cannot be sourced** and are dropped from the new
display tabs; replacements that *can* be computed are listed in PLAN.md.

## 13. Google Sheets

Service account auth from `GOOGLE_APPLICATION_CREDENTIALS` works; the test
sheet ("Copy of AMF CFB Matchups", timezone America/Chicago) was read with
`spreadsheets.get` + `values.batchGet`. Structure summary (full dump saved
for Phase 2):

- 50 tabs; 37 hidden weekly "Week N Targets" / 2024 tabs.
- `DK ` (main, 3 frozen rows, 4 frozen cols, 7 conditional formats): A1 "Last
  Updated: 9/1 1pm"; rows 2–3 headers; column A = slate label rows (`Thurs`,
  `Fri`, `Main`) interleaved with team abbreviations; every other column is an
  `IFERROR(VLOOKUP(...))` into `Vegas (ref)`, `2025 Pace (ref)`, `EPA Raw
  (ref)`.
- `2025 Targets` / `2025 Passing` / `2025 Rushing`: B1 is a `ONE_OF_RANGE`
  data-validation dropdown over a PFF raw tab's team column; A4 holds a
  `QUERY(...)` into the PFF raw tab; extra columns compute Tgt %, TPRR, WOPR,
  percentiles.
- `(ref)` tabs each have one protected range with no listed editors
  (owner-only); `EPA Raw (ref)` still points at cfb-graphs URLs.
- Existing `Vegas (ref)` is a paste of the teamrankings odds page with
  `#N/A`/`#VALUE!` cells, so it is not a stable target for writes.

No `Log` or `Config` tab exists yet.

## Not found / not sourceable

| Need | Status | Proposal |
|------|--------|----------|
| PFF team/player grades, routes, YPRR, slot/wide %, TTT, BTT%, TWP%, pressure rates | No free source | Placeholder tab + manual CSV ingest; new tabs omit these columns |
| Per-book lines beyond DraftKings | ESPN exposes DK only | CFBD `/lines` when key works; The Odds API (`americanfootball_ncaaf`, `apiKey` query param, free 500 credits/month, DraftKings included) documented as an optional third source, not wired |
| Explicit QB-scramble flag | Not in any feed | Rule: `rush` where rusher is the game's passer/QB and text lacks "kneel" → dropback bucket; documented, toggleable |
| Player positions in play-by-play | Not on play rows | Join `espn_cfb_rosters` / `game_rosters` release; unknown → blank position, not guessed |
| CFBD anything | Key rejected | Regenerate key; everything else is live now |

## Dead sources confirmed dead

- `cfb-graphs.com` — referenced in your EPA tab; not used.
- `sportsdataverse/cfbfastR-data` in-tree parquet — ends at 2021; README
  itself redirects to the release-based pipeline used above.
- CFBD v1 host/paths — removed before 2025 season.

## Addendum 2026-09-09 (Phase 4): team metrics moved to CFBD /plays + /drives

While wiring pace and EPA, the sportsdataverse `espn_cfb_pbp` 2026 file turned
out to be badly incomplete for week 1: 33 of 97 games had fewer than 60
scrimmage plays (e.g. Boston College–Cincinnati had 15 plays total) and 18
games had stale play clocks. CFBD `/plays?year=2026&week=1` (confirmed live,
23 MB, 35,631 plays, 203 games) had every game, with `ppa` on 99.7% of
scrimmage plays, and CFBD `/drives` (4,852 drives) had start/end/elapsed clocks.

Decision: **pace, EPA and RROE now read CFBD `/plays` and `/drives`** (one call
per week each; completed weeks cached 14 days, the current week 6 hours, so a
daily rebuild costs about 3–5 CFBD calls). EPA = CFBD `ppa`. The sportsdataverse
parquet feeds remain available in `sources/sportsdataverse.py` and will be
re-evaluated for player names in Phase 6 against CFBD `/passing/plays` and
`/rushing/plays`.

Play-level clocks are stale in about half of all games in **both** feeds (they
share ESPN as the origin), so pace is computed from drive elapsed time rather
than play-to-play clock deltas; see `transform/pace.py` for the exact rule.

Validation on the dev sheet: our offense EPA/dropback, EPA/rush and defense
EPA/dropback correlate 0.95 / 0.94 / 0.95 with CFBD's own `/ppa/teams` season
aggregates (garbage time excluded) across all 138 FBS teams.
