# RROE model quality report

Trained 2026-09-09 22:35 CDT on seasons [2025, 2026].

| | value |
|---|---|
| training plays / games | 79,441 / 809 |
| holdout plays / games (grouped by game) | 19,446 / 202 |
| base rush rate | 0.5057 |
| holdout log loss (model / constant baseline) | 0.6169 / 0.6931 |
| holdout Brier (model / baseline) | 0.2146 / 0.25 |

Features: down, distance, yards_to_goal, score_diff, secs_left_half, half, period, timeouts, spread_off, total, has_line

## Calibration (holdout, 10 bins of predicted P(rush))

| bin | plays | mean predicted | actual rush rate |
|---|---|---|---|
| 0.0-0.1 | 132 | 0.082 | 0.098 |
| 0.1-0.2 | 1,653 | 0.159 | 0.159 |
| 0.2-0.3 | 1,743 | 0.248 | 0.242 |
| 0.3-0.4 | 1,926 | 0.35 | 0.357 |
| 0.4-0.5 | 2,688 | 0.457 | 0.472 |
| 0.5-0.6 | 5,883 | 0.55 | 0.554 |
| 0.6-0.7 | 2,740 | 0.643 | 0.637 |
| 0.7-0.8 | 1,384 | 0.743 | 0.733 |
| 0.8-0.9 | 983 | 0.857 | 0.839 |
| 0.9-1.0 | 314 | 0.924 | 0.914 |

Population: eligible dropback/rush plays (no garbage time, kneels, spikes, no-play
penalties) with an FBS offense. Team RROE% uses 1st and 2nd downs only.
