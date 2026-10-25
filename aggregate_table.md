## Metadata Columns (not stats — for reference and grouping)

| column | description | source | role |
|--------|------------|--------|------|
| pitcher_id | MLB player ID (key_mlbam) | Chadwick register | Primary key (with year) |
| player_name | Full name | FanGraphs | Display |
| year | Season year (2015–2025) | Partition key | Primary key (with pitcher_id) |
| league | AL or NL | See league rule below | Group key (with year) — never a model feature |
| team | Team abbreviation | FanGraphs | Display |
| age | Player age during season | FanGraphs | Display only — never a model feature |
| cy_young_votes | Actual Cy Young vote points received (0 if none) | `pylahman.AwardsSharePlayers()` → `pointsWon`, filtered to `awardID == 'Cy Young Award'` | Training label — decides who beat whom in each pair |
| cy_young_grade | Relevance grade: 4 = winner, 3 = 2nd, 2 = 3rd–5th, 1 = 6th or lower, 0 = no votes | Derived from cy_young_votes within league-year | Scoring label for NDCG |
| cy_young_rank | Rank by vote points within league-year | Derived from cy_young_votes | Display and Kendall's tau |

## Data Rules

**Sources.** Five pulls cover every stat and label:

| pull | call | joins to pitcher_id via |
|------|------|-------------------------|
| FanGraphs | `pitching_stats(2015, 2025, qual=0, ind=1)` — one call for all years | `IDfg` = Chadwick `key_fangraphs` → `key_mlbam` |
| BBRef | `pitching_stats_bref(year)` — one call per year | `mlbID` (already key_mlbam) |
| BBRef WAR file | `bwar_pitch(return_all=True)` — one call covers bWAR and ERA+ | `mlb_ID` (already key_mlbam) |
| Savant | `statcast_pitcher_expected_stats(year)` — one call per year | `player_id` (already key_mlbam) |
| Voting | `pylahman.AwardsSharePlayers()` | `playerID` → `pylahman.People().bbrefID` = Chadwick `key_bbref` → `key_mlbam`. Never join `playerID` straight to `key_bbref`: they differ for some players (`montafr01` vs `montafr02`), and `smithca05` silently matches the wrong player |

`qual=0` matters: without it, FanGraphs returns qualified pitchers only, and relievers and low-IP starters silently vanish. All joins are exact — no fuzzy matching.

**Rename map.** Each pull gets one dictionary mapping its raw column names to the names in the Stats table below (e.g. FanGraphs `ShO` → `SHO`, Savant `est_woba` → `xwOBA`). Renamed columns are noted in the source column of the Stats table.

**Join check.** Every Cy Young vote-getter from 2015–2025 must land on exactly one row. Lahman and BBRef IDs agree almost always, but not always — an assertion catches the odd one out instead of silently dropping a contender.

**Coverage check.** After renaming, an assertion confirms every stat exists and is non-null for every year 2015–2025. A stat that fails gets dropped from the feature pool rather than patched. Likely casualties: Stuff+, Location+ and Pitching+, which FanGraphs may only carry from 2020.

**League rule.** League = the league of the last team pitched for. Exception: a vote-getter takes Lahman `lgID`, because that is the ballot the pitcher actually appeared on. Any vote-getter where the two disagree gets logged.

**Direction.** Up = higher is better, Down = lower is better, Middle = no expected sign. Direction is only used as a sanity check on the final weights (an ERA weight pointing the wrong way means something upstream broke). Middle stats are left out of that check.

## Scraping Patches (as of 2026-10-18)

FanGraphs, BBRef, and BBRef WAR are all behind Cloudflare challenges. The `pybaseball` calls in the Sources table above no longer work. Workarounds:

| original call | status | workaround |
|---------------|--------|------------|
| `pitching_stats(2015, 2025, qual=0, ind=1)` | **Blocked** — FanGraphs legacy endpoint returns 403, new endpoint behind Cloudflare | Playwright + `playwright_stealth`: load the HTML leaderboard page per year (`?month=0&pos=all&stats=pit&type=8&pageitems=2000000000&startdate=&enddate=&season1={year}&season={year}`), clear Cloudflare manually, intercept the `/api/leaders/major-league/data` response. One year per browser launch — FanGraphs aggregates multi-year ranges as rolling averages. `startdate=&enddate=` params must be present (even empty) to scope to regular season only |
| `pitching_stats_bref(year)` | **Broken** — HTML parsing fails (no `<table>` found) | 2B, 3B, GIDP available from MLB Stats API (`statsapi.mlb.com/api/v1/stats?stats=season&group=pitching&season={year}&sportId=1`) — free, no auth, no Cloudflare |
| `bwar_pitch(return_all=True)` | **Blocked** — BBRef behind Cloudflare | Same Playwright approach as FanGraphs. bWAR cannot be computed manually |
| `statcast_pitcher_expected_stats(year)` | **Works** | No change needed |
| `pylahman.AwardsSharePlayers()` | **Works** | No change needed |

## Stats

Every stat belongs to exactly one scope; a stat with no close relatives is a scope of one. Phase 2 keeps one stat per scope.

Source: blank = FanGraphs `pitching_stats()`, same column name. Anything else is spelled out, with the raw column name in brackets when it differs.

| stats | stats_full | source | direction | scope |
|-------|-----------|--------|-----------|-------|
| W | Wins | | Up | Win record |
| L | Losses | | Down | Win record |
| WPCT | Win Percentage | Derived: W / (W + L) | Up | Win record |
| ERA | Earned Run Average | | Down | Run prevention |
| R | Runs Allowed | | Down | Run prevention |
| ER | Earned Runs | | Down | Run prevention |
| ERA+ | Adjusted ERA Plus | BBRef WAR file (`ERA_plus`) | Up | Run prevention |
| ERA- | ERA Minus | | Down | Run prevention |
| FIP | Fielding Independent Pitching | | Down | Run estimators |
| xFIP | Expected Fielding Independent Pitching | | Down | Run estimators |
| SIERA | Skill-Interactive ERA | | Down | Run estimators |
| xERA | Expected ERA | Savant (`xera`) | Down | Run estimators |
| FIP- | FIP Minus | | Down | Run estimators |
| fWAR | FanGraphs Wins Above Replacement | (`WAR`) | Up | WAR |
| bWAR | Baseball Reference Wins Above Replacement | BBRef WAR file (`WAR`) | Up | WAR |
| G | Games Played | | Up | Workload |
| GS | Games Started | | Up | Workload |
| IP | Innings Pitched | | Up | Workload |
| NP | Number of Pitches | (`Pitches`) | Middle | Workload |
| TBF | Total Batters Faced | | Middle | Workload |
| CG | Complete Games | | Up | Durability |
| SHO | Shutouts | (`ShO`) | Up | Durability |
| SV | Saves | | Up | Saves |
| SVO | Save Opportunities | Derived: SV + BS | Up | Saves |
| SO | Strikeouts | | Up | Strikeout ability |
| K/9 | Strikeouts per 9 Innings | | Up | Strikeout ability |
| K% | Strikeout Percentage | | Up | Strikeout ability |
| SwStr% | Swinging Strike Percentage | | Up | Strikeout ability |
| CSW% | Called Strike Plus Whiff Percentage | (`C+SwStr%`) | Up | Strikeout ability |
| BB | Bases on Balls | | Down | Walk control |
| IBB | Intentional Bases on Balls | | Down | Walk control |
| BB/9 | Walks per 9 Innings | | Down | Walk control |
| BB% | Walk Percentage | | Down | Walk control |
| K/BB | Strikeout-to-Walk Ratio | | Up | Strikeout-walk balance |
| K-BB% | Strikeout Minus Walk Percentage | | Up | Strikeout-walk balance |
| H | Hits Allowed | | Down | Hits allowed |
| H/9 | Hits per 9 Innings | | Down | Hits allowed |
| AVG | Batting Average Against | | Down | Hits allowed |
| HR | Home Runs Allowed | | Down | Home runs |
| HR/9 | Home Runs per 9 Innings | | Down | Home runs |
| HR/FB | Home Run to Fly Ball Ratio | | Down | Home runs |
| WHIP | Walks plus Hits per Inning Pitched | | Down | Baserunner prevention |
| OBP | On-Base Percentage Against | Derived: (H + BB + HBP) / (AB + BB + HBP + SF), with AB and SF from BBRef | Down | Baserunner prevention |
| SLG | Slugging Percentage Against | Derived: (1B + 2×2B + 3×3B + 4×HR) / AB, where 1B = H − 2B − 3B − HR | Down | Offense against |
| OPS | On-Base Plus Slugging Against | Derived: OBP + SLG | Down | Offense against |
| xBA | Expected Batting Average Against | Savant (`est_ba`) | Down | Expected offense against |
| xSLG | Expected Slugging Against | Savant (`est_slg`) | Down | Expected offense against |
| xwOBA | Expected wOBA Against | Savant (`est_woba`) | Down | Expected offense against |
| GB% | Ground Ball Percentage | | Middle | Batted ball profile |
| FB% | Fly Ball Percentage | | Middle | Batted ball profile |
| LD% | Line Drive Percentage | | Down | Batted ball profile |
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
| S% | Strike Percentage | Derived: Strikes / NP × 100 | Up | First pitch effectiveness |
| F-Strike% | First Pitch Strike Percentage | | Up | First pitch effectiveness |
| P/IP | Pitches per Inning Pitched | Derived: NP / IP | Down | Pitch efficiency |
| P/PA | Pitches per Plate Appearance | Derived: NP / TBF | Down | Pitch efficiency |
| Stuff+ | Stuff Plus | | Up | Pitch quality |
| Location+ | Location Plus | | Up | Pitch quality |
| Pitching+ | Pitching Plus | | Up | Pitch quality |
| WPA | Win Probability Added | | Up | Win probability |
| RE24 | Run Expectancy 24 Base-Out States | | Up | Win probability |
| HLD | Holds | | Up | Holds |
| QS | Quality Starts | | Up | Quality starts |
| 2B | Doubles Allowed | BBRef | Down | Doubles allowed |
| 3B | Triples Allowed | BBRef | Down | Triples allowed |
| HBP | Hit By Pitch | | Down | Hit batters |
| BABIP | Batting Average on Balls in Play | | Middle | BABIP |
| GIDP | Grounded Into Double Play | BBRef (`GDP`) | Up | Double plays |
| WP | Wild Pitches | | Down | Wild pitches |
| BK | Balks | | Down | Balks |
| IFFB% | Infield Fly Ball Percentage | | Up | Infield flies |

## Removed Stats

Dropped for being costly to compute (pitch-by-pitch tracking or `des` text parsing) and very unlikely to sway a Cy Young voter:

| stats | reason |
|-------|--------|
| SB, CS, PK | Baserunning against — fragile `des` parsing (e.g. "picked off and caught stealing" double-counts), negligible voter weight |
| IR_S | Inherited runners scored — needs runner tracking across pitcher changes, reliever-only niche |
| BQR, BQR_S | Bequeathed runners — same runner tracking, same niche |
