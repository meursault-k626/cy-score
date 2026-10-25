Updated daily:
- Cy Young race ranking: https://549maji0ad.execute-api.ap-southeast-2.amazonaws.com/cy-summary
- Full Cy Young race ranking - https://549maji0ad.execute-api.ap-southeast-2.amazonaws.com/cy-rankings

*(add `?format=xlsx` to download as xlsx)*

Cy Young race ranking (summary) columns

| Column | Description |
|---|---|
| player_name | Pitcher's name |
| current_team | Current team (updates automatically if traded) |
| league | AL or NL |
| p_throws | Handedness — R or L |
| as_of_date | Date rankings are current as of |
| cy_race_rank | based on cy_race_score |
| GS | Games started |
| IP | Innings pitched |
| K | Strikeouts |
| official_W | W |
| official_L | L |
| ERA | Earned run average |
| WHIP | Walks plus hits per inning pitched |
| FIP | Fielding independent pitching — ERA-scale metric based only K, BB, HR — aka Three True Outcomes (TTO) |
| FIP_ERA_gap | FIP minus ERA — positive means ERA likely to rise; negative means pitcher has been unlucky and ERA likely to fall |
| xwOBA_against | Expected weighted on-base average against, based on exit velocity and launch angle |
| hard_hit_pct | Share of balls in play hit at 95+ mph exit velocity |
| barrel_pct | Share of balls in play that are barrelled |
| GB_pct | Ground ball rate |
| K_pct | Strikeout rate (K per batter faced) |
| BB_pct | Walk rate (BB per batter faced) |
| swstr_pct | Swinging strike rate |
| cy_race_score | Raw unified model score |

Cy Race Score weightage

| Tier | Feature | Weight | Direction | Why? |
|---|---|---|---|---|
| Core | ERA | -2.5 | Lower is better | Earned run average — base stats for pitchers |
| Core | IP | 2.0 | Higher is better | Innings pitched (to fulfil unspoken rule of min 160~ IP requirements for Cy consideration) |
| Core | FIP | -2.0 | Lower is better | TTO strips out luck, good defence — current voters are leaning more towards it |
| Secondary | K_pct | 1.5 | Higher is better | Rate-based dominance — preferred over raw K as it adjusts for workload |
| Secondary | BB_pct | -1.5 | Lower is better | Conceptually similar to K_pct — command is entirely pitcher-controlled with no defensive noise |
| Secondary | xwOBA_against | -1.5 | Lower is better | Best Statcast contact quality metric — captures what ERA misses when defence bails out or fails a pitcher |
| Secondary | WHIP | -1.2 | Lower is better | Partially redundant with ERA but distinct enough — second most visible stat in broadcast coverage |
| Supporting | swstr_pct | 1.0 | Higher is better | Less noisy than K_pct as a stuff indicator — leading signal that stabilises earlier in the season |
| Supporting | delta_run_exp | -1.0 | Lower is better | Context-aware run prevention — rewards getting out of jams, not just accumulating clean innings |
| Supporting | K | 0.8 | Higher is better | Reduced because K_pct and IP already capture this — small weight nods to stat-line visibility voters track |
| Supporting | barrel_pct | -0.8 | Lower is better | Partially captured by xwOBA_against — modest independent signal for hard contact suppression |
| Context | opponent_wOBA | 0.5 | Higher is better | Schedule strength should nudge rankings, not swing them — lowest weight by design |

Weights are normalised to sum to 1.0 before scoring. Features scaled using RobustScaler (median and IQR) to limit outlier influence.
