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
|  aggregate_table.md                 |
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
+-------------------------------------+
                   |
                   v
           Final weights go
        back into the lambda
```


## Phase 1 — Aggregation

This is where `aggregate_pitcher_yearly.py` lives. It pulls season stats for each
year, rolls everything up to one row per pitcher per season, and writes a parquet
file. `aggregate_table.md` is the contract: every column, where it comes from, how
the sources join, and the checks that run after the joins.

Two choices behind that spec are worth knowing:

- **Ready-made stats over home-made ones.** FanGraphs and BBRef already publish
  IP, H, R, SO and friends. Rebuilding them from pitch-by-pitch Statcast is slower
  and easy to get subtly wrong — counting IP from the `events` column, for example,
  misses outs from caught stealing and pickoffs. So the raw Statcast pull is not
  needed for training.
- **Exact joins only.** Every source maps to the MLB numeric ID through a shared
  ID, never by matching names. A name match would happily confuse two pitchers
  called Will Smith.

### Who Is in Each Group?

Each league-year group needs a defined candidate pool. Rule: every pitcher who
received at least one vote, plus every pitcher with at least 1 IP per 3 games the
team has played (54 IP over a full 162-game season, 20 over 2020's 60 games).
"Qualified pitchers only" would leave out relievers who got votes — Emmanuel
Clase finished 3rd in the 2024 AL vote with 74.1 IP.


## Phase 2 — Feature Selection

### The Problem

The stats table holds far too many columns for 22 league-year groups (11 years ×
2 leagues). Worse, many of them measure the same underlying skill — the "scope"
column in `aggregate_table.md` maps this out. Feeding them all into a model creates
**multicollinearity**: the model cannot tell which version of "strikeout ability"
actually matters.

Multicollinearity is like asking five witnesses who all saw the same event from
the same angle. Each testimony sounds different, but the information is identical.
A jury (the model) gets confused about which witness to trust, when in reality one
clear account would suffice. In concrete terms: SO, K/9, K%, and SwStr% all spike
and dip for the same pitchers in the same years. Including all four dilutes the
signal rather than strengthening it.

### Step 1 — Standardise Within Each League-Year

Before anything else, every stat gets converted to a z-score *within its own
league-year group*: how many standard deviations above or below that group's
average a pitcher sits.

This is like grading on a curve. A 2.50 ERA in the 2019 juiced-ball season is far
more impressive than a 2.50 ERA in the pitcher-friendly 2022 season, and a z-score
captures exactly that. It also handles 2020's 60-game season: counting stats (IP,
SO, W) are all small in 2020, but z-scores compare each pitcher only to that same
shortened season.

### Step 2 — Pick One Stat per Scope

Each scope in `aggregate_table.md` keeps exactly one member: the stat that, on its
own, best ranks the training groups. A scope of one keeps its only member
automatically.

The test is cheap. A one-stat ranker has only one real choice — sort the group
high-to-low or low-to-high — so each candidate's score is simply NDCG@5
(explained in Phase 3) from sorting each group by that stat, in whichever
direction the training years favour.

```
Strikeout ability scope:
+------+------+-----+--------+------+la
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

SO might beat K%, for example, because a raw strikeout total rewards both skill
*and* workload, which voters like.

### Actual vs Expected

Scopes keep actual results and expected results apart. "Run prevention" holds
ERA, ERA+ and friends; "Run estimators" holds FIP, xFIP, SIERA, xERA. Lumped
together, ERA would almost certainly beat FIP — voters reward what actually
happened — and the estimators would never get a fair hearing. Split apart, each
gets its own representative and the model decides whether the expected side adds
anything.

### Everything Learned Happens Inside the Fold

Feature selection, the regularisation setting `C` and the weights are all learned
from data, so all three run *inside* every LOOCV fold (Phase 4), using only that
fold's 10 training years. Picking features with all 11 years first and then
validating is like a teacher writing the exam after seeing the answer sheet — the
score looks great and means nothing. Z-scores are the one exception: each only
uses its own league-year group, so they are safe to compute up front.

A side effect worth watching: different folds may pick different winners for the
same scope. Which member goes into the final model is a judgment call, handed to
the agent in Phase 5.


## Phase 3 — Learning to Rank

### What Is "Learning to Rank"?

Most ML problems are about predicting a number (regression) or a category
(classification). This problem is neither — it's about predicting an **ordering**.
Pitcher A should rank above Pitcher B, who should rank above Pitcher C.

Analogy: a tennis ranking built only from head-to-head results. Nobody hands out
an overall score; the ranking falls out of who beat whom. Concrete example: in
2021, NL voters ranked Corbin Burnes (ERA 2.43, K% 35.6%) above Zack Wheeler
(ERA 2.78, K% 29.1%). That is one head-to-head result. The model sees thousands of
them and learns how much each stat difference tends to decide the winner.

### Why a Linear Model?

**Small data.** 22 groups, each with roughly 5–15 vote-getters — about 165–275
labelled pitchers in total. A flexible model (like a gradient-boosted forest of
hundreds of trees) would memorise those groups rather than learn anything general.

**The output has to be weights.** A linear model *is* a weighted sum:
`score = w₁·z₁ + w₂·z₂ + …`. Positive means "more of this helps a Cy Young case",
negative means it hurts, and the size says how much. Those coefficients *are* the
Cy Score formula. Tree models only offer "feature importances", which count how
often a stat was used to split — they have no sign and don't add up to a score.

### Pairwise Logistic Regression

The method turns ranking into a pile of yes/no questions that ordinary logistic
regression (from `scikit-learn`) can answer.

1. Within each league-year group, take every pair of pitchers with *different*
   vote points. Pairs where both got 0 votes say nothing and get skipped.
2. For each pair, subtract one pitcher's z-scores from the other's.
3. The label is 1 if the first pitcher got more vote points, otherwise 0.
4. Fit logistic regression with no intercept. The coefficients are the weights.

A typical group of 60 candidates with 10 vote-getters yields about 545 pairs
(10 × 50 vote-getter vs non-vote-getter pairs, plus 45 vote-getter vs vote-getter
pairs), so roughly 12,000 pairs over 22 groups.

**Regularisation.** L2 regularisation (the `C` setting) shrinks weights of
unhelpful stats towards zero, which keeps the model from overfitting 22 groups.
`C` is the only real setting to tune.

### Ranking Metric: NDCG

NDCG — Normalised Discounted Cumulative Gain — scores how good a ranking is.

The idea is simple: getting the top of the ranking right matters more than getting
the bottom right. Predicting the correct Cy Young *winner* is worth more than
correctly placing the pitcher who finished 12th. NDCG bakes this priority in — it
discounts errors the further down the ranking they occur. Like how a newspaper
prints the winner on page one and buries 12th place in the agate section; the top
spots carry disproportionate weight.

NDCG needs a relevance grade per pitcher rather than raw vote points — see
`cy_young_grade` in `aggregate_table.md`. NDCG@5 looks only at the top 5 of the
predicted ranking, the part anyone actually reads.

### League-Agnostic Weights

The Cy Young Award is voted separately per league (one AL winner, one NL winner),
but the formula should not care about league. ERA matters the same amount whether
the pitcher plays in the AL or NL.

Pairs only ever come from inside one league-year group ("2021-NL", "2021-AL",
etc.), so the model only compares pitchers who were actually ranked against each
other by voters. But the weights it learns are global — one set of weights across
all groups. The formula is league-blind; the display is league-aware.

### Optional Benchmark: LambdaRank

LightGBM's `LGBMRanker` (LambdaRank) is a tree-based ranker that optimises NDCG
directly. It cannot produce weights, but it makes a useful benchmark: if the trees
barely beat the linear model in LOOCV, the linear model is leaving little on the
table. If the trees win by a mile, voters care about an interaction that a
weighted sum cannot express. Only worth running once the linear pipeline works end
to end.


## Phase 4 — Validation (LOOCV)

### What Is LOOCV?

Leave-One-Out Cross-Validation. With 11 years of data, the model trains on 10
years and predicts the 11th. Then rotates — trains on a different 10, predicts the
remaining 1. Repeat 11 times so every year gets a turn as the test case.
(`scikit-learn` calls this `LeaveOneGroupOut`, with year as the group.)

Analogy: a study group preparing for an exam. Each week, one person sits out while
the other ten write practice questions. The person who sat out takes the test.
Rotate until everyone has been tested on questions they didn't help write. If
everyone passes, the group's knowledge is solid — not just memorised from one
specific practice set.

Holding out a year means holding out both its AL and NL groups: the model trains
on 20 groups and predicts 2. Selection, `C` and weights are all re-learned inside
each fold (see Phase 2).

Concrete example: train on 2015–2024, predict 2025. Did the model rank Tarik
Skubal as the AL winner and Paul Skenes as the NL winner? Then train on 2015–2023
+ 2025, predict 2024. Did it get Skubal (AL) and Chris Sale (NL)? And so on back
to 2015. If one year consistently fails (say 2020, the 60-game COVID season),
that's a signal to investigate.

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

**Top-1 accuracy** — did the model pick the actual winner? Out of 22 groups, how
many winners did it name? The simplest number, and the one that matters most.

**NDCG@5** — how good is the top 5? Same metric as Phase 2 feature selection, so
selection and validation pull in the same direction.

**Kendall's tau-b** — how well does the whole ordering of vote-getters match? It
works by examining every possible pair of pitchers.

For each pair, one question: did the model and the voters agree on who ranked
higher? If yes, that's a **concordant** pair. If no, that's a **discordant** pair.

```
Actual vote (illustrative order):  Burnes > Wheeler > Scherzer > Woodruff > Gausman
Model output:                      Burnes > Scherzer > Wheeler > Woodruff > Gausman
```

Out of the 10 possible pairs from 5 pitchers:

- 9 concordant (model and voters agree)
- 1 discordant (the Wheeler/Scherzer pair is the only one flipped)

Tau = (concordant - discordant) / total pairs = (9 - 1) / 10 = **0.8**

Tau is calculated only among pitchers who received votes. Across the whole
candidate pool, most pitchers sit tied at 0 votes, and the plain tau formula
mishandles ties — the **tau-b** variant (the default in `scipy.stats.kendalltau`)
corrects for them.

```
-1.0          0.0          +1.0
  |------------|------------|
perfectly    random       perfect
backwards                agreement
```

### Targets: Beat a Baseline First

Rather than picking target numbers out of thin air, the first run compares against
two dumb baselines:

- **ERA only** — rank every group by ERA, lowest first.
- **fWAR only** — rank every group by fWAR, highest first.

If the learned weights cannot beat "just sort by fWAR", the extra machinery isn't
earning its keep. Concrete targets get set after seeing how the baselines score.


## Phase 5 — The Agentic Loop

### Why an Agent?

After every LOOCV run, the results raise questions without clean programmatic
answers. Without an agent, each one is either manual intervention (stop, inspect,
decide, restart) or a hardcoded rule written in advance (brittle, breaks on edge
cases). The agent sits in the middle — it reads the actual output, reasons about
it, and picks a path from a pre-built menu.

### What the Agent Decides

Five jobs. Each lists what Claude sees and what it can choose.

**1. Diagnose the misses.** Scores alone say *that* a fold went wrong, not *why*.
So Claude sees the misranked pitchers themselves: predicted rank, actual rank,
role (starter or reliever) and their selected stats. From the patterns, it picks a
fix: change the candidate pool rule, add a combined stat from the pre-built list,
weight pairs by grade gap, or change the grade bands.

Concrete example: in 4 folds, closers land in the predicted top 5 while voters put
them 8th or lower. Claude reads that as SV dominating for relievers and adds the
pre-built "SV × reliever flag" combined stat so the model can learn a separate
reliever discount.

**2. Settle scope disagreements.** Folds may pick different members of the same
scope. Claude sees each fold's winner and its NDCG@5 margin over the runner-up.
Choices: fix one member for every fold, drop the scope, or keep letting folds
choose.

Concrete example: ERA+ wins 6 folds by a hair, ERA wins 5 by a wide margin. A
majority vote would pick ERA+; Claude can weigh the margins and pick ERA instead.

**3. Check the weight signs.** Claude sees the final weights next to the
`direction` column (Middle stats excluded). For each weight pointing the wrong
way, it picks one: stop and report a likely data bug, drop one of two
overlapping stats that are fighting each other, or accept it as a genuine voter
quirk (logged with the reason).

Concrete example: WHIP comes out with a positive weight while ERA is strongly
negative. The two overlap heavily, so the model has offset one against the other.
Claude drops WHIP's scope and re-runs.

**4. Compare against the baselines and the benchmark.** Claude sees LOOCV scores
for the linear model, ERA-only, fWAR-only and LambdaRank, plus LambdaRank's most
used stat pairs. Choices: add a combined stat that captures what the trees found,
or stop because the linear model is close enough.

Concrete example: LambdaRank beats the linear model by 0.08 NDCG@5 and splits
on IP straight after ERA in most trees. Claude adds the pre-built "IP × ERA-"
combined stat, so the linear model can reward low ERA over a big workload.

**5. Write the run report.** At END, Claude writes a plain-English summary: every
round, what changed, why, and how the scores moved. The weights come with their
reasoning attached.

### Guard Rails

An agent that keeps tweaking until LOOCV looks good ends up tuning the pipeline to
those 11 years specifically. Four rules keep it honest:

1. **A hard cap on iterations** (say 5). The loop ends at the cap whether or not
   the agent is happy.
2. **Every decision is logged** — what the agent saw, what it chose, and why.
3. **Menu only.** Claude replies with structured output — an option ID from the
   adjustment menu plus a reason. Anything not on the menu gets rejected, and the
   conditional edge routes on the option ID.
4. **General fixes only** — nothing that singles out one year because that year
   scored badly.

### LangChain vs LangGraph

**LangChain** is a toolkit of components for building LLM-powered applications:
chains, agents, prompt templates, document loaders. Most of it is irrelevant here.
**LangGraph** is a separate library from the same company, focused on **stateful
workflows with loops**, and works without the rest of LangChain. Regular chains
are linear (step A, then B, then C); LangGraph lets step C loop back to step B
based on a decision — exactly what the Cy Score pipeline needs.
**langchain-anthropic** is the connector that lets LangGraph talk to Claude.

### How LangGraph Works

Three building blocks:

**State** — a shared dictionary that persists across the entire run. Every node
reads from it and writes to it. It carries the pipeline's running memory: current
feature picks, current weights, LOOCV scores, the iteration count, the agent's
decision log.

**Nodes** — functions that do one thing. Some are pure Python (crunch data, train
models), some call Claude (make decisions). Each node receives the current state,
does its work, and returns updates to the state.

**Edges** — connections between nodes. Normal edges always proceed ("after
training, always run LOOCV"). Conditional edges branch based on state ("if LOOCV
passed or the cap is hit, go to END; if not, go to adjust"). These conditional
edges are where Claude's judgment controls the flow.

Analogy: a choose-your-own-adventure book. The story (pipeline) moves forward page
by page (node by node), and at certain points there's a fork: "turn to page 47 to
retry, or page 83 to continue." Instead of a reader making the choice, Claude
reads the results and decides which page to turn to.

Concrete example: after the LOOCV node runs, LangGraph passes the scores, feature
picks and misranked pitchers to Claude. Claude spots the closer pattern from job 1
and replies `{"option": "add_combined_stat", "stat": "SV_x_reliever", "reason":
"..."}`. LangGraph routes to the adjustment node, applies that pre-built option,
then loops back to training.

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
|                                    |  report   |        |
|                                    | (Claude)  |        |
|                                    +-----+-----+        |
|                                          v              |
|                                        END              |
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

Nodes marked **(Python)** run entirely on the laptop — pandas, scikit-learn,
parquet reads and writes. No network calls, no data leaving the machine.

The **review** and **report (Claude)** nodes send a small JSON payload to
`api.anthropic.com` — LOOCV scores, current weights, feature picks, benchmark
scores and the misranked pitchers (a few dozen rows) — and get a structured
decision or the report back. The full stats table never leaves the laptop.

### All Scripts Must Be Written First

Claude does not write or modify code during the pipeline run. The agent only reads
outputs and picks pre-built paths, so everything the pipeline *could* do must be
written in advance:

- `aggregate_pitcher_yearly.py` — aggregation logic
- Training script — feature selection, pairwise logistic regression, the `C`
  search, the LambdaRank benchmark
- Adjustment script — the menu below
- The LangGraph graph definition — all nodes, all edges, all possible paths

The adjustment menu (each option has an ID the agent replies with):

| option | what it does | agent job |
|--------|--------------|-----------|
| `change_pool` | Switch the candidate pool rule (e.g. raise the IP threshold, or starters only plus vote-getters) | 1 |
| `add_combined_stat` | Add one stat from a pre-built list of combinations (e.g. IP × ERA-, SV × reliever flag, K% × IP) | 1, 4 |
| `weight_pairs_by_grade` | Weight each pair by the gap between the two grades, so winner-vs-runner-up pairs count more | 1 |
| `change_grade_bands` | Swap to an alternative set of `cy_young_grade` bands | 1 |
| `fix_scope_member` | Use one named member of a scope in every fold | 2 |
| `drop_scope` | Remove a scope from the feature pool | 2, 3 |
| `free_scope` | Undo a fix and let folds choose again | 2 |
| `stop_data_bug` | End the loop and report a suspected data problem | 3 |
| `accept_sign` | Keep a wrong-way weight as a genuine voter quirk | 3 |
| `stop_converged` | End the loop — results are good enough | 4 |

The more options on the menu, the more flexible the agent becomes.


## Efficiency Notes

None of these change the results — they only stop the pipeline doing the same work
twice.

- **Feature selection is a lookup.** A one-stat ranker only chooses a sort
  direction, so NDCG@5 can be computed once per group, stat and direction. Each
  fold then sums the scores for its training groups instead of fitting a model.
- **Pairs are built once.** Every pair-difference row across all stats (about
  12,000 rows, roughly 8 MB) gets built once. Each fold, `C` value and agent pass
  only filters rows and columns.
- **`C` search uses `LogisticRegressionCV`**, with year-based inner splits passed
  through its `cv` argument — tuning inside each outer fold without a hand-written
  loop.
- **Data pulls happen once.** FanGraphs is one call for all years
  (`pitching_stats(2015, 2025, qual=0, ind=1)`). The Chadwick register gets loaded
  once and saved to disk. `pybaseball.cache.enable()` covers the rest. The agent
  loop reads the saved parquet and never re-pulls.


## Package Map

| Package | Role | Phase |
|---------|------|-------|
| `pandas` | Data manipulation | 1–4 |
| `pybaseball` | Pull season stats (FanGraphs, BBRef, Savant) + Chadwick ID register | 1 |
| `pylahman` | Cy Young voting data (`AwardsSharePlayers`) | 1 |
| `pyarrow` | Read and write parquet files | 1–4 |
| `scikit-learn` | Pairwise logistic regression, `LeaveOneGroupOut`, `ndcg_score` | 2–4 |
| `scipy` | Kendall's tau-b (`scipy.stats.kendalltau`) | 4 |
| `lightgbm` | LambdaRank benchmark | 3 (optional) |
| `langgraph` | Stateful workflow graph with loops | 5 |
| `langchain-anthropic` | Claude API integration for LangGraph | 5 |

Phases 1–4 need no LLM, no LangGraph and no API key. The pipeline is fully
functional without the agent — the agent only makes it autonomous. Start without
it, add it once the core pipeline is solid.
