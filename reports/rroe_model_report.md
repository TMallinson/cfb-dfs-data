# RROE model quality report

Trained 2026-09-14 11:26 CDT on seasons [2025, 2026].

| | value |
|---|---|
| training plays / games | 84,474 / 878 |
| holdout plays / games (grouped by game) | 21,038 / 219 |
| base rush rate | 0.5028 |
| holdout log loss (model / constant baseline) | 0.6137 / 0.693 |
| holdout Brier (model / baseline) | 0.2134 / 0.2499 |

Features: down, distance, yards_to_goal, score_diff, secs_left_half, half, period, timeouts, spread_off, total, has_line

## Calibration (holdout, 10 bins of predicted P(rush))

| bin | plays | mean predicted | actual rush rate |
|---|---|---|---|
| 0.0-0.1 | 130 | 0.083 | 0.108 |
| 0.1-0.2 | 1,704 | 0.159 | 0.162 |
| 0.2-0.3 | 1,979 | 0.249 | 0.268 |
| 0.3-0.4 | 1,951 | 0.352 | 0.357 |
| 0.4-0.5 | 2,768 | 0.457 | 0.471 |
| 0.5-0.6 | 6,540 | 0.551 | 0.552 |
| 0.6-0.7 | 3,198 | 0.643 | 0.657 |
| 0.7-0.8 | 1,487 | 0.744 | 0.748 |
| 0.8-0.9 | 1,031 | 0.852 | 0.882 |
| 0.9-1.0 | 250 | 0.927 | 0.976 |

Population: eligible dropback/rush plays (no garbage time, kneels, spikes, no-play
penalties) with an FBS offense. Team RROE% uses 1st and 2nd downs only.
