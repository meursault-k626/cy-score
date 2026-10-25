## Metadata Columns (not stats — for reference, grouping, and role context)

| column | description | source | role |
|--------|------------|--------|------|
| pitcher_id | MLB player ID (`key_mlbam`) | Chadwick register | Primary key (with `year`) |
| player_name | Full name | FanGraphs | Display |
| year | Season year (2015–2025) | Partition key | Primary key (with `pitcher_id`) |
| league | AL or NL | See league rule below | Group key (with `year`) — never a model feature |
| team | Team abbreviation (`- - -` if multi-team) | FanGraphs | Display |
| age | Player age during season | FanGraphs | Display only — never a model feature |
| is_reliever | Role flag: `1` if `GS == 0` or `(SV + HLD) > GS`, else `0` | Derived from FanGraphs `GS`, `SV`, `HLD` | Candidate pool filtering & role interaction terms |
| cy_young_votes | Actual Cy Young vote points received (0 if none) | `pylahman.AwardsSharePlayers()` → `pointsWon`, filtered to `awardID == 'Cy Young Award'` (+ local supplement CSV if `pylahman` lags) | Training label — decides who beat whom in each pair |
| cy_young_grade | Relevance grade: 4 = winner, 3 = 2nd, 2 = 3rd–5th, 1 = 6th or lower, 0 = no votes | Derived from `cy_young_votes` within league-year | Scoring label for NDCG and pair-loss weighting |
| cy_young_rank | Rank by vote points within league-year | Derived from `cy_young_votes` | Display and Kendall's tau-b |

## Data Rules

**Sources.** Five pulls cover every stat and label:

| pull | call | joins to `pitcher_id` via |
|------|------|---------------------------|
| FanGraphs | `pitching_stats(2015, 2025, qual=0, ind=1)` — one call for all years | `IDfg` = Chadwick `key_fangraphs` → `key_mlbam` |
| MLB Stats API | `statsapi.mlb.com/api/v1/stats?stats=season&group=pitching&season={year}&sportId=1&gameType=R&limit=2000` — one call per year | `player.id` (already `key_mlbam`) |
| BBRef WAR file | `bwar_pitch(return_all=True)` — one call covers `bWAR` and `ERA+` | `mlb_ID` (already `key_mlbam`) |
| Savant | `statcast_pitcher_expected_stats(year, minPA=1)` — one call per year | `player_id` (already `key_mlbam`) |
| Voting | `pylahman.AwardsSharePlayers()` + `cy_young_votes_supplement.csv` for any season > `AwardsSharePlayers()['yearID'].max()` | `playerID` → `pylahman.People().bbrefID` = Chadwick `key_bbref` → `key_mlbam`. Never join `playerID` straight to `key_bbref`: they differ for some players (`montafr01` vs `montafr02`), and `smithca05` silently matches the wrong player |

`qual=0` on FanGraphs and `minPA=1` on Savant both matter: without `qual=0`, FanGraphs returns qualified pitchers only; without `minPA=1`, Savant defaults to `minPA='q'` and drops relievers and low-IP starters, which would cause the coverage check below to discard every Savant stat (`xERA`, `xBA`, `xSLG`, `xwOBA`). All joins are exact — no fuzzy matching.

**Innings conversion.** Baseball shorthand stores partial innings as `.1` (one out, $\frac{1}{3}$ inning) and `.2` (two outs, $\frac{2}{3}$ inning). Every `IP` column is converted to true decimal innings ($\text{whole} + \frac{\text{tenths}}{3}$) immediately on load — before applying the candidate pool threshold, weighting `ERA+` across stints, deriving `P/IP`, or computing `IP / max_IP`.

**Multi-stint aggregation.** FanGraphs (`ind=1`) returns a single combined season row per pitcher (`team = "- - -"` for mid-season trades). However, `bwar_pitch` and the MLB Stats API return one row per team stint when a pitcher changes teams. Before joining any secondary pull to the FanGraphs spine on `(pitcher_id, year)`, stint-level rows are collapsed to one row per `(pitcher_id, year)`:
1. **Counting stats** (`bWAR`, `2B`, `3B`, `GIDP`, `AB`, `SF`) are summed across stints.
2. **`ERA+` (IP-weighted harmonic mean):** Because $\text{ERA+} = 100 \times \frac{\text{lgERA}}{\text{ERA}}$ has `ERA` in the denominator, combining stints requires an IP-weighted harmonic mean ($\frac{\sum \text{IP}_i}{\sum (\text{IP}_i / \text{ERA+}_i)}$, using true decimal `IP`). If a short stint has `0 ER` (`ERA+` is null/infinite in `bwar_pitch`), those innings still add to the numerator while adding $0$ to the denominator sum $\sum (\text{IP}_i / \text{ERA+}_i)$ (since $\text{ER}_i = 0$ adds zero earned runs); if a pitcher has `0 ER` across the entire season, `ERA+` is capped at the season maximum (`999`). `OBP`, `SLG`, and `OPS` are taken from the MLB Stats API combined season split (or computed from summed `H`, `2B`, `3B`, `HR`, `BB`, `HBP`, `AB`, `SF`).
3. **Last-team league** is recorded from the final chronological stint in that season before collapsing, so traded pitchers (`team = "- - -"`) have a deterministic league.

**Rename map.** Each pull gets one dictionary mapping its raw column names to the names in the Stats table below (e.g. FanGraphs `ShO` → `SHO`, Savant `est_woba` → `xwOBA`, MLB Stats API `groundIntoDoublePlay` → `GIDP`). Renamed columns are noted in the source column of the Stats table.

**Join check.** After joining all five pulls:
1. An assertion confirms zero duplicate `(pitcher_id, year)` rows (`df.duplicated(subset=['pitcher_id', 'year']).sum() == 0`).
2. Every Cy Young vote-getter from 2015–2025 must land on exactly one row. Lahman and BBRef IDs agree almost always, but not always — an assertion catches the odd one out instead of silently dropping a contender.

**Coverage check.** After renaming, an assertion confirms every stat exists and is non-null across the candidate pool for every year 2015–2025. A stat that fails gets dropped from the feature pool rather than patched. Likely casualties: `Stuff+`, `Location+`, and `Pitching+`, which FanGraphs only carries from 2020.

**League rule.** League = the league of the last team pitched for in that season. Exception: a vote-getter takes Lahman `lgID`, because that is the ballot the pitcher actually appeared on. Any vote-getter where the two disagree gets logged.

**Direction.** `Up` = higher is better, `Down` = lower is better, `Middle` = no expected sign. Direction is only used as a sanity check on the final weights (an ERA weight pointing the wrong way means something upstream broke). `Middle` stats are left out of that check.

## Scraping Patches (as of 2026-10-18)

FanGraphs, BBRef, and BBRef WAR are all behind Cloudflare challenges. Workarounds:

| original call | status | workaround |
|---------------|--------|------------|
| `pitching_stats(2015, 2025, qual=0, ind=1)` | **Blocked** — FanGraphs legacy endpoint returns 403, new endpoint behind Cloudflare | Playwright + `playwright_stealth`: load the HTML leaderboard page per year (`?month=0&pos=all&stats=pit&type=8&pageitems=2000000000&startdate=&enddate=&season1={year}&season={year}`), clear Cloudflare manually, intercept the `/api/leaders/major-league/data` response, and save to disk. One year per browser launch — FanGraphs aggregates multi-year ranges as rolling averages. `startdate=&enddate=` params must be present (even empty) to scope to regular season only |
| `pitching_stats_bref(year)` | **Broken** — HTML parsing fails (no `<table>` found) | `2B`, `3B`, `GIDP`, `AB`, `SF`, `OBP`, `SLG`, `OPS` pulled from MLB Stats API (`statsapi.mlb.com/api/v1/stats?stats=season&group=pitching&season={year}&sportId=1&gameType=R&limit=2000`) — free, no auth, no Cloudflare |
| `bwar_pitch(return_all=True)` | **Blocked** — BBRef (`war_daily_pitch.txt`) behind Cloudflare | Same Playwright approach as FanGraphs to download `war_daily_pitch.txt` once and cache locally. `bWAR` cannot be computed manually |
| `statcast_pitcher_expected_stats(year, minPA=1)` | **Works** | Pass `minPA=1` so non-qualified pitchers are returned |
| `pylahman.AwardsSharePlayers()` | **Works** | Supplement with local CSV if `pylahman` lags the latest 2024–2025 seasons |

## Stats

Every stat belongs to one of **21 conceptual scopes** (20 once `Pitch quality` is dropped on pre-2020 coverage), each grouping competing measures of the same underlying skill. Phase 2 keeps one stat per scope.

Source: blank = FanGraphs `pitching_stats()`, same column name. Anything else is spelled out, with the raw column/field name in parentheses when it differs.

| stats | stats_full | source | direction | scope |
|-------|-----------|--------|-----------|-------|
| W | Wins | | Up | Win record |
| L | Losses | | Down | Win record |
| WPCT | Win Percentage | Derived: `W / (W + L)` (0 if `W + L == 0`) | Up | Win record |
| ERA | Earned Run Average | | Down | Run prevention |
| R | Runs Allowed | | Down | Run prevention |
| ER | Earned Runs | | Down | Run prevention |
| ERA+ | Adjusted ERA Plus | BBRef WAR file (`ERA_plus`, IP-weighted harmonic mean across stints) | Up | Run prevention |
| ERA- | ERA Minus | | Down | Run prevention |
| FIP | Fielding Independent Pitching | | Down | Run estimators |
| xFIP | Expected Fielding Independent Pitching | | Down | Run estimators |
| SIERA | Skill-Interactive ERA | | Down | Run estimators |
| xERA | Expected ERA | Savant (`xera`) | Down | Run estimators |
| FIP- | FIP Minus | | Down | Run estimators |
| fWAR | FanGraphs Wins Above Replacement | (`WAR`) | Up | WAR |
| bWAR | Baseball Reference Wins Above Replacement | BBRef WAR file (`WAR`, summed across stints) | Up | WAR |
| G | Games Played | | Up | Workload |
| GS | Games Started | | Up | Workload |
| IP | Innings Pitched | | Up | Workload |
| NP | Number of Pitches | (`Pitches`) | Middle | Workload |
| TBF | Total Batters Faced | | Middle | Workload |
| QS | Quality Starts | | Up | Durability |
| CG | Complete Games | | Up | Durability |
| SHO | Shutouts | (`ShO`) | Up | Durability |
| SV | Saves | | Up | Relief value |
| SVO | Save Opportunities | Derived: `SV + BS` | Up | Relief value |
| HLD | Holds | | Up | Relief value |
| SO | Strikeouts | | Up | Strikeout ability |
| K/9 | Strikeouts per 9 Innings | | Up | Strikeout ability |
| K% | Strikeout Percentage | | Up | Strikeout ability |
| SwStr% | Swinging Strike Percentage | | Up | Strikeout ability |
| CSW% | Called Strike Plus Whiff Percentage | (`C+SwStr%`) | Up | Strikeout ability |
| BB | Bases on Balls | | Down | Walk & command control |
| IBB | Intentional Bases on Balls | | Down | Walk & command control |
| BB/9 | Walks per 9 Innings | | Down | Walk & command control |
| BB% | Walk Percentage | | Down | Walk & command control |
| HBP | Hit By Pitch | | Down | Walk & command control |
| WP | Wild Pitches | | Down | Walk & command control |
| BK | Balks | | Down | Walk & command control |
| K/BB | Strikeout-to-Walk Ratio | | Up | Strikeout-walk balance |
| K-BB% | Strikeout Minus Walk Percentage | | Up | Strikeout-walk balance |
| H | Hits Allowed | | Down | Hits allowed |
| H/9 | Hits per 9 Innings | | Down | Hits allowed |
| AVG | Batting Average Against | | Down | Hits allowed |
| 2B | Doubles Allowed | MLB Stats API (`doubles`) | Down | Hits allowed |
| 3B | Triples Allowed | MLB Stats API (`triples`) | Down | Hits allowed |
| BABIP | Batting Average on Balls in Play | | Middle | Hits allowed |
| HR | Home Runs Allowed | | Down | Home runs |
| HR/9 | Home Runs per 9 Innings | | Down | Home runs |
| HR/FB | Home Run to Fly Ball Ratio | | Down | Home runs |
| WHIP | Walks plus Hits per Inning Pitched | | Down | Baserunners & offense against |
| OBP | On-Base Percentage Against | MLB Stats API (`obp`, or derived `(H + BB + HBP) / (AB + BB + HBP + SF)`) | Down | Baserunners & offense against |
| SLG | Slugging Percentage Against | MLB Stats API (`slg`, or derived `(1B + 2×2B + 3×3B + 4×HR) / AB`) | Down | Baserunners & offense against |
| OPS | On-Base Plus Slugging Against | MLB Stats API (`ops`, or derived `OBP + SLG`) | Down | Baserunners & offense against |
| xBA | Expected Batting Average Against | Savant (`est_ba`) | Down | Expected offense against |
| xSLG | Expected Slugging Against | Savant (`est_slg`) | Down | Expected offense against |
| xwOBA | Expected wOBA Against | Savant (`est_woba`) | Down | Expected offense against |
| GB% | Ground Ball Percentage | | Middle | Batted ball & double plays |
| FB% | Fly Ball Percentage | | Middle | Batted ball & double plays |
| LD% | Line Drive Percentage | | Down | Batted ball & double plays |
| IFFB% | Infield Fly Ball Percentage | | Up | Batted ball & double plays |
| GIDP | Grounded Into Double Play | MLB Stats API (`groundIntoDoublePlay`) | Up | Batted ball & double plays |
| Soft% | Soft Contact Percentage | | Up | Contact quality |
| Hard% | Hard Contact Percentage | | Down | Contact quality |
| Barrel% | Barrel Percentage | | Down | Contact quality |
| HardHit% | Hard Hit Percentage | | Down | Contact quality |
| EV | Average Exit Velocity Against | | Down | Contact quality |
| O-Swing% | Outside Zone Swing Rate | | Up | Plate discipline |
| Z-Swing% | Zone Swing Rate | | Middle | Plate discipline |
| O-Contact% | Outside Zone Contact Rate | | Down | Plate discipline |
| Z-Contact% | Zone Contact Rate | | Down | Plate discipline |
| Contact% | Overall Contact Rate | | Down | Plate discipline |
| Zone% | Zone Percentage | | Middle | Plate discipline |
| S% | Strike Percentage | Derived: `Strikes / NP × 100` | Up | First pitch & strike rate |
| F-Strike% | First Pitch Strike Percentage | | Up | First pitch & strike rate |
| P/IP | Pitches per Inning Pitched | Derived: `NP / IP` | Down | Pitch efficiency |
| P/PA | Pitches per Plate Appearance | Derived: `NP / TBF` | Down | Pitch efficiency |
| Stuff+ | Stuff Plus | | Up | Pitch quality |
| Location+ | Location Plus | | Up | Pitch quality |
| Pitching+ | Pitching Plus | | Up | Pitch quality |
| WPA | Win Probability Added | | Up | Win probability |
| RE24 | Run Expectancy 24 Base-Out States | | Up | Win probability |

## Pre-Built Combined Stats (Phase 5 Menu Only)

These combined terms are **not** included in Phase 2 scope selection. They sit in reserve for the Phase 5 agent (`add_combined_stat`). To prevent two negative z-scores from multiplying into a positive score for a low-IP, poor-rate pitcher, the workload/role multiplier in each term is **non-negative** (`IP_share = IP / max_IP_in_group`, sitting in `(0, 1]`, or `is_reliever ∈ {0, 1}`). When enabled by the agent, the term is computed, z-scored within the league-year group, and added directly to the Phase 3 feature set:

| term | definition (before final within-group z-score) | direction | role |
|------|------------------------------------------------|-----------|------|
| `IP_x_ERA_inv` | `(IP / max_IP) × (-z(ERA))` | Up | Rewards maintaining a low ERA over a starter's full workload without rewarding low-IP, high-ERA pitchers |
| `K_pct_x_IP` | `(IP / max_IP) × z(K%)` | Up | Rewards high strikeout rate scaled by innings share |
| `SV_x_reliever` | `is_reliever × z(SV)` | Up | Isolates closer save credit to relievers (`0` for starters) |
| `IP_x_reliever` | `is_reliever × (IP / max_IP)` | Middle | Allows a separate innings slope for relievers vs starters |

## Removed Stats

Dropped for being costly to compute (pitch-by-pitch tracking or `des` text parsing) and very unlikely to sway a Cy Young voter:

| stats | reason |
|-------|--------|
| SB, CS, PK | Baserunning against — fragile `des` parsing (e.g. "picked off and caught stealing" double-counts), negligible voter weight |
| IR_S | Inherited runners scored — needs runner tracking across pitcher changes, reliever-only niche |
| BQR, BQR_S | Bequeathed runners — same runner tracking, same niche |
