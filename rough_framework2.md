# Rough Framework: Cy Score Pipeline

## The Goal

The Cy Young Award is voted on by the Baseball Writers' Association of America. The current `lambda_function_statcast.py` scores pitchers using vibe-based weights — numbers picked by feel rather than evidence. The goal is to replace those vibes with data-driven weights learned from 11 years (2015–2025) of actual voting results.

The end product: a single set of weights applied to pitcher stats that, when used to rank all candidate pitchers in a given year, produces an ordering closely matching how voters actually voted.

## The Pipeline (Bird's Eye)

```
+-------------------------------------+
|  aggregate_pitcher_yearly.py        |   Phase 1
|  Build the stats table:             |
|  one row per pitcher per year,      |
|  2015-2025                          |
+------------------+------------------+
                   |
                   v
+------------------+------------------+
|  Feature Selection                  |   Phase 2
|  Pick one stat per scope from       |
|  aggregate_table2.md                |
+------------------+------------------+
                   |
                   v
+------------------+------------------+
|  Train: linear pairwise ranker      |   Phase 3
|  Learn weights that rank pitchers   |
|  the way voters do                  |
+------------------+------------------+
                   |
                   v
+------------------+------------------+
|  Validate: LOOCV                    |   Phase 4
|  Leave one year out, check if       |
|  weights generalise                 |
+------------------+------------------+
                   |
                   v
+------------------+------------------+
|  Agentic Loop: LangGraph + Claude   |   Phase 5
|  Evaluate results, adjust,          |
|  re-run until converged             |
+------------------+------------------+
                   |
                   v
     Train final weights on all
     11 years -> export to lambda
```


## Phase 1 — Aggregation

This is where `aggregate_pitcher_yearly.py` lives. It pulls season stats for each year, collapses multi-team stints, rolls everything up to one row per pitcher per season, and writes a parquet file. `aggregate_table2.md` is the contract: every column, where it comes from, how the sources join, and the checks that run after the joins.

Three choices behind that spec are worth knowing:

- **Ready-made stats over home-made ones.** FanGraphs, the MLB Stats API, and BBRef already publish `IP`, `H`, `R`, `SO`, `OBP`, `SLG`, and friends. Rebuilding them from pitch-by-pitch Statcast is slower and easy to get subtly wrong — counting `IP` from the `events` column, for example, misses outs from caught stealing and pickoffs. So the raw Statcast pitch-by-pitch pull is not needed for training.
- **Exact joins only.** Every source maps to the MLB numeric ID (`key_mlbam`) through a shared register ID, never by matching names. A name match would happily confuse two pitchers called Will Smith.
- **Collapse multi-team stints before joining.** FanGraphs (`ind=1`) returns one combined season row per pitcher (`team = "- - -"` when traded mid-season), whereas `bwar_pitch` and the MLB Stats API return one row per team stint. Before joining to the FanGraphs spine on `(pitcher_id, year)`, stint-level rows are aggregated to a single row per pitcher-year (summing counting stats like `bWAR`, `2B`, `3B`, `GIDP`, and taking the IP-weighted harmonic mean of `ERA+`) while recording the league of the last team pitched for.

### Who Is in Each Group?

Each league-year group needs a defined candidate pool. Rule: every pitcher who received at least one vote, plus every pitcher with at least `1 IP` per 3 league-average team games played to date (`54 IP` over a completed 162-game season, `20 IP` over 2020's 60 games, and scaled automatically in April or May when the live lambda runs mid-season). Using the league-average team games played avoids ambiguity for pitchers traded between teams that have played slightly different game counts. Baseball's `.1` ($\frac{1}{3}$) and `.2` ($\frac{2}{3}$) innings notation is converted to true decimal innings on load before checking the threshold. "Qualified pitchers only" (`1 IP` per team game) would leave out relievers who got votes — Emmanuel Clase finished 3rd in the 2024 AL vote with `74.1 IP`.


## Phase 2 — Feature Selection

### The Problem

The stats table holds far too many columns for 22 league-year groups (11 years × 2 leagues). Worse, many of them measure the same underlying skill — the `scope` column in `aggregate_table2.md` maps all 81 stats into 21 conceptual scopes (20 after `Pitch quality` is dropped on pre-2020 coverage). Every stat sits in a scope with competing peers rather than getting a free pass as a scope of one. Feeding all 81 columns into a model creates **multicollinearity**: the model cannot tell which version of "strikeout ability" actually matters.

Multicollinearity is like asking five witnesses who all saw the same event from the same angle. Each testimony sounds different, but the information is identical. A jury (the model) gets confused about which witness to trust, when in reality one clear account would suffice. In concrete terms: `SO`, `K/9`, `K%`, and `SwStr%` all spike and dip for the same pitchers in the same years. Including all four dilutes the signal rather than strengthening it.

### Step 1 — Standardise Within Each League-Year

Before anything else, every stat gets converted to a z-score *within its own league-year group*: how many standard deviations above or below that group's average a pitcher sits.

This is like grading on a curve. A `2.50 ERA` in the 2019 juiced-ball season is far more impressive than a `2.50 ERA` in the pitcher-friendly 2022 season, and a z-score captures exactly that. It also handles 2020's 60-game season: counting stats (`IP`, `SO`, `W`) are all small in 2020, but z-scores compare each pitcher only to that same shortened season.

### Step 2 — Pick One Stat per Scope

Each scope in `aggregate_table2.md` keeps exactly one member: the stat that, on its own, best ranks the training groups.

The test is cheap. A one-stat ranker has only one real choice — sort the group high-to-low or low-to-high — so each candidate's score is simply `NDCG@5` (explained in Phase 3) from sorting each group by that stat, in whichever direction the training years favour.

```
Strikeout ability scope:
+------+------+-----+--------+------+
|  SO  | K/9  | K%  | SwStr% | CSW% |
+------+------+-----+--------+------+
   |      |     |       |       |
   v      v     v       v       v
 [score each one alone on the training years]
                |
                v
          Winner: e.g. SO
     (whichever one best tracks
       voter rankings)
```

`SO` might beat `K%`, for example, because a raw strikeout total rewards both skill *and* workload, which voters like.

### Actual vs Expected

Scopes keep actual results and expected results apart. `Run prevention` holds `ERA`, `R`, `ER`, `ERA+`, and `ERA-`; `Run estimators` holds `FIP`, `xFIP`, `SIERA`, `xERA`, and `FIP-`. Similarly, `Baserunners & offense against` (`WHIP`, `OBP`, `SLG`, `OPS`) is separate from `Expected offense against` (`xBA`, `xSLG`, `xwOBA`). Lumped together, `ERA` would almost certainly beat `FIP` — voters reward what actually happened — and the estimators would never get a fair hearing. Split apart, each gets its own representative and the model decides whether the expected side adds anything.

### Everything Learned Happens Inside the Fold

Feature selection, the regularisation setting `C`, and the weights are all learned from data, so all three run *inside* every LOOCV fold (Phase 4), using only that fold's 10 training years. Picking features with all 11 years first and then validating is like a teacher writing the exam after seeing the answer sheet — the score looks great and means nothing. Z-scores are the one exception: each only uses its own league-year group, so they are safe to compute up front.

A side effect worth watching: different folds may pick different winners for the same scope. Which member goes into the final model is a judgment call, handed to the agent in Phase 5.


## Phase 3 — Learning to Rank

### What Is "Learning to Rank"?

Most ML problems are about predicting a number (regression) or a category (classification). This problem is neither — it's about predicting an **ordering**. Pitcher A should rank above Pitcher B, who should rank above Pitcher C.

Analogy: a tennis ranking built only from head-to-head results. Nobody hands out an overall score; the ranking falls out of who beat whom. Concrete example: in 2021, NL voters ranked Corbin Burnes (`ERA 2.43`, `K% 35.6%`) above Zack Wheeler (`ERA 2.78`, `K% 29.1%`). That is one head-to-head result. The model sees thousands of them and learns how much each stat difference tends to decide the winner.

### Why a Linear Model?

**Small data.** 22 groups, each with roughly 5–15 vote-getters — about 165–275 labelled pitchers in total. A flexible model (like a gradient-boosted forest of hundreds of trees) would memorise those groups rather than learn anything general.

**The output has to be weights.** A linear model *is* a weighted sum: `score = w₁·z₁ + w₂·z₂ + …`. Positive means "more of this helps a Cy Young case", negative means it hurts, and the size says how much. Those coefficients *are* the Cy Score formula. Tree models only offer "feature importances", which count how often a stat was used to split — they have no sign and don't add up to a score.

### Pairwise Logistic Regression

The method turns ranking into a pile of yes/no questions that ordinary logistic regression (from `scikit-learn`) can answer.

1. **Build symmetric pairs.** Within each league-year group, take every unordered pair of pitchers $\{A, B\}$ with *different* vote points ($\text{votes}_A > \text{votes}_B$). Pairs where both got 0 votes say nothing and get skipped. For each pair, emit **both** directions:
   - Row 1: $z_A - z_B$ with label `1` ($A$ beat $B$)
   - Row 2: $z_B - z_A$ with label `0` ($B$ lost to $A$)
   Including both directions gives an exact 50/50 split of `1`s and `0`s (avoiding a single-class error when fitting with no intercept).
2. **Weight pairs toward the top of the ballot.** A typical group of 60 candidates (1 winner at grade `4`, 1 runner-up at grade `3`, 3 at grade `2`, 5 at grade `1`, and $N_0 = 50$ pitchers with 0 votes at grade `0`) yields 545 unique pairs: 45 compare two vote-getters (including 13 pairs within the same grade band that have different vote points), while 500 (`91.7%`) compare a vote-getter against one of the 50 unvoted pitchers.
   Weighting pairs of two vote-getters by $2^{\text{grade}_A} + 2^{\text{grade}_B}$ gives the 45 contender-vs-contender pairs a total weight of **`414`**, with `1st vs 2nd` (`16 + 8 = 24`) as the single heaviest pair.
   For the 500 pairs against the unvoted field ($\text{grade}_B = 0$), dividing $2^{\text{grade}_A}$ by $N_0$ gives a field total of **`46`** — putting `90.0%` of the weight on contender pairs, `10.0%` on field pairs, and `5.2%` on `1st vs 2nd` alone. However, because `NDCG@5` ranks the entire 60-pitcher pool, shrinking the field share all the way to `10%` risks letting a high-`K%`, low-`IP` unvoted reliever drift into the predicted top 5. Dividing field pairs by $\sqrt{N_0}$ instead strikes a middle balance:
   $$w_{\text{pair}}(A, B) = \begin{cases} 2^{\text{grade}_A} + 2^{\text{grade}_B} & \text{if both got votes } (\text{grade}_A, \text{grade}_B > 0) \\ \dfrac{2^{\text{grade}_A}}{\sqrt{N_0}} & \text{if } B \text{ got 0 votes } (\text{grade}_B = 0) \end{cases}$$
   Under $\sqrt{N_0}$ ($\sqrt{50} \approx 7.07$):
   - All 45 pairs of two vote-getters: total weight $\mathbf{414}$ (`56.0%` of the group's weight).
   - All 500 pairs of vote-getter vs no-vote pitcher: total weight $46 \times \sqrt{50} \approx \mathbf{325.3}$ (`44.0%` of the group's weight).
   - `1st vs 2nd` (1 pair): weight $\mathbf{24}$ (`3.2%` of the group's weight, still the single heaviest pair).
   The Phase 5 `weight_pairs_by_grade` option lets the agent switch between `/ √N₀` (default), `/ N₀`, and unweighted pairs.
3. **Fit L1-regularised logistic regression with no intercept.** Even after picking one stat per scope in Phase 2, 20 scopes remain and several still overlap across scope boundaries (`WAR`, `Run prevention`, `Baserunners & offense against`). L1 regularisation (`penalty='l1'`, `solver='liblinear'`, `fit_intercept=False`) shrinks unhelpful or redundant scopes all the way to `0.0`, keeping the final formula compact without needing a separate feature-cutoff threshold in Phase 2.
4. **Tune `C` on `NDCG@5` via an inner loop (with inner feature selection).** `C` is the only hyperparameter to tune. Rather than using `LogisticRegressionCV`'s built-in scorer — which scores pair-classification accuracy rather than pitcher rankings — a small inner `LeaveOneGroupOut` loop over the 10 training years holds out one inner year at a time. Inside each 9-year inner split, Phase 2 scope selection is re-computed on those 9 inner years (so the inner validation year never leaks through the selected scope winners), a grid of `C` values (e.g. 10 log-spaced values) is fitted, and the held-out inner year's pitcher rankings are scored on `NDCG@5`. The `C` with the highest mean inner `NDCG@5` is then used to refit on all 10 training years of the outer fold. That way Phase 2 selection, Phase 3 `C` tuning, and Phase 4 validation all optimise the exact same ranking target.

### Ranking Metric: NDCG

`NDCG` — Normalised Discounted Cumulative Gain — scores how good a ranking is.

The idea is simple: getting the top of the ranking right matters more than getting the bottom right. Predicting the correct Cy Young *winner* is worth more than correctly placing the pitcher who finished 12th. NDCG bakes this priority in — it discounts errors the further down the ranking they occur. Like how a newspaper prints the winner on page one and buries 12th place in the agate section; the top spots carry disproportionate weight.

NDCG needs a relevance grade per pitcher rather than raw vote points — see `cy_young_grade` in `aggregate_table2.md`. `NDCG@5` looks only at the top 5 of the predicted ranking, the part anyone actually reads.

### League-Agnostic Weights

The Cy Young Award is voted separately per league (one AL winner, one NL winner), but the formula should not care about league. `ERA` matters the same amount whether the pitcher plays in the AL or NL.

Pairs only ever come from inside one league-year group (`2021-NL`, `2021-AL`, etc.), so the model only compares pitchers who were actually ranked against each other by voters. But the weights it learns are global — one set of weights across all groups. The formula is league-blind; the display is league-aware.

### Optional Benchmark: LambdaRank

LightGBM's `LGBMRanker` (LambdaRank) is a tree-based ranker that optimises NDCG directly. It cannot produce weights, but it makes a useful benchmark: if the trees barely beat the linear model in LOOCV, the linear model is leaving little on the table. If the trees win by a mile, voters care about an interaction that a weighted sum cannot express. Only worth running once the linear pipeline works end to end.


## Phase 4 — Validation (LOOCV) & Final Training

### What Is LOOCV?

Leave-One-Out Cross-Validation. With 11 years of data, the model trains on 10 years and predicts the 11th. Then rotates — trains on a different 10, predicts the remaining 1. Repeat 11 times so every year gets a turn as the test case. (`scikit-learn` calls this `LeaveOneGroupOut`, with `year` as the group.)

Analogy: a study group preparing for an exam. Each week, one person sits out while the other ten write practice questions. The person who sat out takes the test. Rotate until everyone has been tested on questions they didn't help write. If everyone passes, the group's knowledge is solid — not just memorised from one specific practice set.

Holding out a year means holding out both its AL and NL groups: the model trains on 20 groups and predicts 2. Feature selection, `C`, and the weights are all re-learned inside each fold (see Phase 2).

Concrete example: train on 2015–2024, predict 2025. Did the model rank Tarik Skubal as the AL winner and Paul Skenes as the NL winner? Then train on 2015–2023 + 2025, predict 2024. Did it get Skubal (AL) and Chris Sale (NL)? And so on back to 2015. If one year fails (say 2016 AL, where Rick Porcello won on 22 wins over Justin Verlander, or 2020's 60-game COVID season), that's a signal to inspect.

```
Illustrative output only:

Year held out:   2015  2016  2017  2018  2019  2020  2021  2022  2023  2024  2025
Winners right:   2/2   1/2   2/2   2/2   2/2   0/2   2/2   2/2   2/2   1/2   2/2
NDCG@5:          .91   .84   .90   .88   .92   .52   .89   .90   .87   .85   .88
Tau-b:           .78   .70   .74   .71   .80   .31   .76   .79   .73   .72   .75
                                               ^^
                                          Problem year — investigate
```

### The Three Scores

**Top-1 accuracy** — did the model pick the actual winner? Out of 22 groups, how many winners did it name? The simplest number, and the one that matters most.

**NDCG@5** — how good is the top 5? Same metric as Phase 2 feature selection and Phase 3 `C` tuning, so selection, tuning, and validation all pull in the same direction.

**Kendall's tau-b** — how well does the whole ordering of vote-getters match? It works by examining every possible pair of pitchers.

For each pair, one question: did the model and the voters agree on who ranked higher? If yes, that's a **concordant** pair. If no, that's a **discordant** pair.

```
Actual vote (illustrative order):  Burnes > Wheeler > Scherzer > Woodruff > Gausman
Model output:                      Burnes > Scherzer > Wheeler > Woodruff > Gausman
```

Out of the 10 possible pairs from 5 pitchers:

- 9 concordant (model and voters agree)
- 1 discordant (the Wheeler/Scherzer pair is the only one flipped)

Tau = `(concordant - discordant) / total pairs = (9 - 1) / 10 = 0.8`.

Tau is calculated only among pitchers who received votes. Across the whole candidate pool, most pitchers sit tied at 0 votes, and the plain tau formula mishandles ties — the **tau-b** variant (the default in `scipy.stats.kendalltau`) corrects for them.

```
-1.0          0.0          +1.0
  |------------|------------|
perfectly    random       perfect
backwards                agreement
```

### Targets: Beat a Baseline First

Rather than picking target numbers out of thin air, the first run compares against two dumb baselines:

- **ERA only** — rank every group by `ERA`, lowest first.
- **fWAR only** — rank every group by `fWAR`, highest first.

If the learned weights cannot beat "just sort by `fWAR`", the extra machinery isn't earning its keep. Concrete targets get set after seeing how the baselines score.

### Final Fit on All 11 Years & Export to the Lambda

LOOCV measures how well a pipeline configuration generalises to unseen years. Once the configuration is locked (either after Phase 4 or at the end of the Phase 5 loop), **the final model is trained once on all 11 years (all 22 league-year groups)** — picking one stat per scope across all 22 groups, tuning `C` via 11-fold year-grouped CV, and fitting the final weights.

Those final weights $w_i$ are exported to `cy_score_weights.json` alongside:
- The live candidate pool rule scaled to the season so far (`1 IP` per 3 league-average team games played to date — `IP >= league_games_played / 3`, which equals `54 IP` at 162 games and scales down in April and May so the live candidate pool is never empty).
- Historical reference averages ($\mu_i$) and spreads ($\sigma_i$) (with counting stats scaled to games played to date) as early-season fallbacks when April within-season z-scores are still noisy.


## Phase 5 — The Agentic Loop

### Why an Agent?

After every LOOCV run, the results raise questions without clean programmatic answers. Without an agent, each one is either manual intervention (stop, inspect, decide, restart) or a hardcoded rule written in advance (brittle, breaks on edge cases). The agent sits in the middle — it reads the actual output, reasons about it, and picks a path from a pre-built menu.

### What the Agent Decides

Five jobs. Each lists what Claude sees and what it can choose.

**1. Diagnose the misses.** Scores alone say *that* a fold went wrong, not *why*. So Claude sees the misranked pitchers themselves: predicted rank, actual rank, role (`is_reliever`), and their selected stats. From the patterns, it picks a fix: change the candidate pool rule (`change_pool`), add a pre-built combined stat from `aggregate_table2.md` (`add_combined_stat`), switch between top-heavy and unweighted pairs (`weight_pairs_by_grade`), or swap the grade bands (`change_grade_bands`).

Concrete example: in 4 folds, closers land in the predicted top 5 while voters put them 8th or lower. Claude reads that as `SV` dominating for relievers and adds the pre-built `SV_x_reliever` combined stat so the model isolates save credit to relievers.

**2. Settle scope disagreements.** Folds may pick different members of the same scope. Claude sees each fold's winner, its `NDCG@5` margin over the runner-up, and how many folds L1 kept it non-zero. Choices: fix one member for every fold (`fix_scope_member`), drop the scope (`drop_scope`), or keep letting folds choose (`free_scope`).

Concrete example: `ERA+` wins 6 folds by a hair, `ERA` wins 5 by a wide margin. A majority vote would pick `ERA+`; Claude can weigh the margins and pick `ERA` instead.

**3. Check the weight signs.** Claude sees the final weights (and fold-level weight stability) next to the `direction` column (`Middle` stats excluded). For each non-zero weight pointing the wrong way, it picks one: stop and report a likely data bug (`stop_data_bug`), drop one of two overlapping scopes that are fighting each other (`drop_scope`), or accept it as a genuine voter quirk logged with the reason (`accept_sign`).

Concrete example: `WHIP` comes out with a positive weight while `ERA` is strongly negative. The two overlap heavily, so the model has offset one against the other. Claude drops `WHIP`'s scope and re-runs.

**4. Compare against the baselines and the benchmark.** Claude sees LOOCV scores for the linear model, `ERA-only`, `fWAR-only`, and `LambdaRank`, plus `LambdaRank`'s most used stat pairs. Choices: add a pre-built combined stat that captures what the trees found (`add_combined_stat`), or stop because the linear model is close enough (`stop_converged`).

Concrete example: `LambdaRank` beats the linear model by `0.08 NDCG@5` and splits on `IP` straight after `ERA` in most trees. Claude adds the pre-built `IP_x_ERA_inv` combined stat (`(IP / max_IP) × (-z(ERA))`), so the linear model can reward maintaining a low `ERA` over a starter's full workload.

**5. Write the run report.** At `END`, after the locked configuration is re-trained on all 11 years, Claude writes a plain-English summary: every round, what changed, why, how the LOOCV scores moved, and the final exported weights with their reasoning attached.

### Guard Rails

With only 22 groups total, carving off a 2-year (4-group) holdout would mean a single flipped winner moves Top-1 accuracy by 25 percentage points while starving training of two seasons. Instead, all 11 years stay in LOOCV, and four rules prevent the agent from overfitting to those 11 years:

1. **A hard cap on iterations** (say 5). The loop ends at the cap whether or not the agent is happy.
2. **Every decision is logged** — what the agent saw, what it chose, and why.
3. **Menu only.** Claude replies with structured output — an option ID from the adjustment menu plus a reason. Anything not on the menu gets rejected, and the conditional edge routes on the option ID.
4. **General fixes only** — nothing that singles out one year because that year scored badly.

### LangChain vs LangGraph

**LangChain** is a toolkit of components for building LLM-powered applications: chains, agents, prompt templates, document loaders. Most of it is irrelevant here. **LangGraph** is a separate library from the same company, focused on **stateful workflows with loops**, and works without the rest of LangChain. Regular chains are linear (step A, then B, then C); LangGraph lets step C loop back to step B based on a decision — exactly what the Cy Score pipeline needs. **langchain-anthropic** is the connector that lets LangGraph talk to Claude.

### How LangGraph Works

Three building blocks:

**State** — a shared dictionary that persists across the entire run. Every node reads from it and writes to it. It carries the pipeline's running memory: current feature picks, current weights, LOOCV scores, the iteration count, the agent's decision log.

**Nodes** — functions that do one thing. Some are pure Python (crunch data, train models), some call Claude (make decisions). Each node receives the current state, does its work, and returns updates to the state.

**Edges** — connections between nodes. Normal edges always proceed ("after training, always run LOOCV"). Conditional edges branch based on state ("if LOOCV passed or the cap is hit, go to final fit & report; if not, go to adjust"). These conditional edges are where Claude's judgment controls the flow.

Analogy: a choose-your-own-adventure book. The story (pipeline) moves forward page by page (node by node), and at certain points there's a fork: "turn to page 47 to retry, or page 83 to continue." Instead of a reader making the choice, Claude reads the results and decides which page to turn to.

Concrete example: after the LOOCV node runs, LangGraph passes the scores, feature picks, and misranked pitchers to Claude. Claude spots the closer pattern from job 1 and replies `{"option": "add_combined_stat", "stat": "SV_x_reliever", "reason": "..."}`. LangGraph routes to the adjustment node, applies that pre-built option, then loops back to training.

### What Runs Where

```
+-------------------------------------------------------+
|                  The Laptop (WSL2)                      |
|                                                         |
|  LangGraph Runtime (just a Python process):             |
|                                                         |
|  +-----------+    +-----------+    +-----------+        |
|  | aggregate |--->|  select + |--->|   LOOCV   |        |
|  | (Python)  |    |   train   |    |  (Python) |        |
|  +-----------+    |  (Python) |    +-----+-----+        |
|                   +-----^-----+          |              |
|                         |                v              |
|                   +-----+-----+    +-----------+        |
|                   |  adjust   |<---|  review   |        |
|                   | (Python)  |    | (Claude)  |        |
|                   +-----------+    +-----+-----+        |
|                                          |              |
|                                          v              |
|                                    +-----------+        |
|                                    | final fit |        |
|                                    | + report  |        |
|                                    | (Claude)  |        |
|                                    +-----+-----+        |
|                                          v              |
|                                         END             |
+-------------------------------------------------------+
                  |                    ^
                  | small JSON         | decision
                  | summary            | (text)
                  v                    |
          +------------------------------+
          |     api.anthropic.com         |
          |      (Claude Sonnet)          |
          +------------------------------+
```

Nodes marked **(Python)** run entirely on the laptop — pandas, scikit-learn, parquet reads and writes. No network calls, no data leaving the machine.

The **review** and **report (Claude)** nodes send a small JSON payload to `api.anthropic.com` — LOOCV scores, current weights, feature picks, benchmark scores, and the misranked pitchers (a few dozen rows) — and get a structured decision or the report back. The full stats table never leaves the laptop.

### All Scripts Must Be Written First

Claude does not write or modify code during the pipeline run. The agent only reads outputs and picks pre-built paths, so everything the pipeline *could* do must be written in advance:

- `aggregate_pitcher_yearly.py` — aggregation, stint collapsing, and join checks
- Training script — feature selection, pairwise L1 logistic regression, the inner `NDCG@5` search for `C`, and the LambdaRank benchmark
- Adjustment script — the menu below
- The LangGraph graph definition — all nodes, all edges, all possible paths

The adjustment menu (each option has an ID the agent replies with):

| option | what it does | agent job |
|--------|--------------|-----------|
| `change_pool` | Switch the candidate pool rule (e.g. raise the IP threshold, or starters only plus vote-getters) | 1 |
| `add_combined_stat` | Add one stat from the pre-built list in `aggregate_table2.md` (`IP_x_ERA_inv`, `K_pct_x_IP`, `SV_x_reliever`, `IP_x_reliever`); bypasses Phase 2 scope selection and joins the active feature set directly | 1, 4 |
| `weight_pairs_by_grade` | Switch pair weighting between `/ √N₀` (default, ~44% field weight), `/ N₀` (10% field weight), and unweighted | 1 |
| `change_grade_bands` | Swap to an alternative set of `cy_young_grade` bands | 1 |
| `fix_scope_member` | Use one named member of a scope in every fold | 2 |
| `drop_scope` | Remove a scope from the feature pool | 2, 3 |
| `free_scope` | Undo a fix and let folds choose again | 2 |
| `stop_data_bug` | End the loop and report a suspected data problem | 3 |
| `accept_sign` | Keep a wrong-way weight as a genuine voter quirk (logged with rationale) | 3 |
| `stop_converged` | End the loop, train final weights on all 11 years, and write the report | 4, 5 |


## Efficiency Notes

None of these change the results — they only stop the pipeline doing the same work twice.

- **Feature selection is a lookup.** A one-stat ranker only chooses a sort direction, so `NDCG@5` can be computed once per group, stat, and direction. Both the outer folds (10 years) and inner `C` splits (9 years) sum the pre-computed scores for their training groups in microseconds instead of fitting a model.
- **Pairs are built once.** Every symmetric pair-difference row across all stats (~24,000 rows across 22 groups, roughly 15 MB) gets built once with its pair weights attached. Each fold, `C` value, and agent pass only filters rows and columns.
- **`C` search uses a small inner loop scored on `NDCG@5`.** Across 11 outer folds × 10 inner folds × 10 `C` values, re-picking scope winners by lookup and fitting `liblinear` on ~20 selected features runs 1,100 lightweight fits — taking a few seconds per full LOOCV pass while tuning `C` on the exact ranking metric (`NDCG@5`) rather than pair accuracy.
- **Data pulls happen once.** Raw Playwright and API pulls are saved to disk as parquet files, and the Chadwick register is cached locally. The agent loop reads the saved parquet and never re-pulls.


## Package Map

| Package | Role | Phase |
|---------|------|-------|
| `pandas` / `numpy` | Data manipulation, stint aggregation, z-scoring | 1–4 |
| `playwright` + `playwright_stealth` | Cloudflare bypass for FanGraphs & BBRef WAR file | 1 |
| `requests` | MLB Stats API (`2B`, `3B`, `GIDP`, `OBP`, `SLG`, `OPS`) | 1 |
| `pybaseball` | Savant expected stats (`minPA=1`) + Chadwick ID register | 1 |
| `pylahman` | Cy Young voting data (`AwardsSharePlayers` + `People`) | 1 |
| `pyarrow` | Read and write parquet files | 1–4 |
| `scikit-learn` | Pairwise logistic regression (`LogisticRegression`), `LeaveOneGroupOut`, `ndcg_score` | 2–4 |
| `scipy` | Kendall's tau-b (`scipy.stats.kendalltau`) | 4 |
| `lightgbm` | LambdaRank benchmark | 3 (optional) |
| `langgraph` | Stateful workflow graph with loops | 5 |
| `langchain-anthropic` | Claude API integration for LangGraph | 5 |

Phases 1–4 need no LLM, no LangGraph, and no API key. The pipeline is fully functional without the agent — the agent only makes it autonomous. Start without it, add it once the core pipeline is solid.
