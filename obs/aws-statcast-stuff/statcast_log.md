# Statcast Pipeline — AWS Build Log

## Overview

This log documents the full build of an automated daily MLB Statcast data pipeline on AWS. The pipeline pulls pitch-level data for all pitchers every day, stores it in the cloud, aggregates it into meaningful statistics, and produces daily Cy Young Award rankings split by league.

The pipeline is designed as a modular pitching database — not solely a Cy Young tool. The architecture is built to support any future pitcher, batter, or team analysis by adding downstream modules without touching the raw data layer.

The pipeline is built entirely on AWS and uses Python as the primary language, replacing an original R approach to take advantage of native AWS support.

---

## Architecture Summary

```
Every day at 8PM Kuala Lumpur time / 10PM Sydney time / 6AM US Eastern (UTC+08:00)
    → EventBridge triggers Lambda (statcast-daily-pull)
        → Lambda pulls all Statcast pitch data for sysdate - 1 day from Baseball Savant
        → MLB Stats API called to retrieve official Earned Runs, Wins, Losses, Saves,
          Holds, and Blown Saves per game
        → Role assigned to each pitcher row (SP or RP)
        → Raw parquet saved to S3 raw/game_date={date}/
        → All raw files loaded and deduplicated
        → Aggregated to one row per pitcher per game, saved to S3 aggregated/pitcher/
        → Dimension tables updated (pitcher, pitcher_team, game, date)
        → CY Young dual model (voter + talent) run on SP regular season data only
        → Daily ranked parquet saved to S3 cy_young/calc_date={date}/
        → Latest rankings file overwritten at cy_young/_cy_young_latest/rankings.parquet

On demand via HTTP request
    → API Gateway receives GET /cy-rankings or GET /cy-summary
        → Triggers respective Lambda function
            → Reads _cy_young_latest/rankings.parquet from S3
            → Returns rankings sorted by cy_rank_voter ascending
            → Response format: JSON (default) or xlsx (?format=xlsx)
```

---

## Data Architecture Principles

The S3 bucket is structured around three logical layers inspired by the medallion architecture pattern:

**Bronze (`raw/`)** — Immutable, append-only pitch-level data. One file per game date. Never modified after write. Single source of truth for all downstream processing.

**Silver (`aggregated/`, `dim/`)** — Derived from raw. Rebuilt or overwritten on each daily run. Cleaned, enriched, and structured for analysis. Dimension tables are full refreshes; the aggregated game log grows incrementally.

**Gold (`cy_young/`)** — Presentation layer. Derived from aggregated. Consumed by the API and downstream tooling. Optimised for specific use cases rather than general querying.

### Hive-style partitioning

Raw and CY Young snapshot files use Hive-style partitioning (`game_date=2026-03-26/`, `calc_date=2026-03-26/`). This allows AWS Athena and Glue to automatically infer the partition column from the folder name without manual mapping, reducing data scanned on date-filtered queries.

---

## Region

**Region selected:** `ap-southeast-2` — Asia Pacific (Sydney)

All AWS services in this project are deployed to the Sydney region. Keeping all services in the same region eliminates cross-region latency and cost, and ensures services can interact with each other directly.

---

## Step 1 — S3 Bucket

**Service:** Amazon Simple Storage Service (S3)
**Bucket name:** `baseball-statcast-data-516769757298-ap-southeast-2-an`

### What S3 is

S3 is object storage — think of it as a filing cabinet in the cloud. Files (called objects) are stored inside buckets (the cabinet itself). S3 is not a database and does not run code; it purely stores and retrieves files.

### Folder structure

```
s3://baseball-statcast-data-516769757298-ap-southeast-2-an/
    raw/
        game_date=2026-03-25/
            statcast_20260325.parquet    ← one row per pitch, all pitchers, that date
        game_date=2026-03-26/
            statcast_20260326.parquet
        ...
    aggregated/
        pitcher/
            pitcher_game_agg_2026.parquet   ← one row per pitcher per game, full season
    cy_young/
        _cy_young_latest/
            rankings.parquet            ← current CY Young standings, overwritten daily
        calc_date=2026-03-26/
            rankings.parquet            ← snapshot of rankings as of that date
        calc_date=2026-03-27/
            rankings.parquet
        ...
    dim/
        dim_pitcher/
            dim_pitcher.parquet         ← one row per pitcher, static attributes
        dim_pitcher_team/
            dim_pitcher_team.parquet    ← one row per pitcher per team stint, tracks trades
        dim_team/
            dim_team.parquet            ← one row per team, reference data
        dim_date/
            dim_date.parquet            ← one row per game date, calendar attributes
        dim_game/
            dim_game.parquet            ← one row per game, home/away teams, game type
        dim_pitch_type/
            dim_pitch_type.parquet      ← one row per pitch type code, full name and category
```

The `_` prefix on `_cy_young_latest/` sorts it to the top in S3, making it immediately visible as the current standings folder. The folder isolation also allows the Glue crawler to be pointed directly at this path, bypassing the historical snapshot subfolders entirely.

The `aggregated/pitcher/` subfolder structure is intentional — when a batter aggregation module is added in future, it will live at `aggregated/batter/` alongside it. Each will have its own dedicated Glue crawler and Athena table, with no schema conflicts.

### Why parquet instead of CSV

Parquet is a columnar file format — it stores data column by column rather than row by row. For analytical workloads such as aggregating statistics, filtering by pitcher, or calculating averages, this is significantly faster and produces smaller file sizes than CSV. It also preserves data types automatically, so numeric columns do not accidentally become strings the way they sometimes do in CSV. Parquet is the standard format used by AWS Athena, Glue, and most data warehousing tools.

### Configuration decisions

| Setting | Choice | Reason |
|---|---|---|
| Namespace | Account Regional | Bucket name scoped to account only, no global naming conflicts |
| Public access | Blocked | Private pipeline, no public access needed |
| Versioning | Disabled | Daily pipeline with date-partitioned files — versioning would silently accumulate copies and consume storage |
| Encryption | SSE-S3 (default) | Encryption at rest with no extra cost or setup — sufficient for non-sensitive sports data |
| Tags | None | Single personal project, no cost allocation tracking needed |

---

## Step 2 — IAM Role

**Service:** AWS Identity and Access Management (IAM)
**Role name:** `lambda-statcast-s3-role`

### What IAM is

IAM is the security and permissions layer of AWS. By default, no AWS service is allowed to interact with any other AWS service. IAM is how permissions are granted explicitly.

Think of it as an access badge system. Every worker (service) needs a badge before it can open any door (interact with any resource). Without a badge, the door stays locked regardless of how legitimate the request is.

### What a role is

A role is an IAM identity that can be assumed by a service. Instead of creating a username and password for Lambda (which would be insecure and hard to rotate), a role is created and attached to Lambda. Lambda then automatically uses that role's permissions when it runs, without any credentials being hardcoded anywhere.

### What it does in this pipeline

The role `lambda-statcast-s3-role` is attached to all Lambda functions. When Lambda tries to read or write a file to S3, AWS checks whether the attached role has permission. Because the role has `AmazonS3FullAccess`, the operation is allowed.

### Policies attached

| Policy | What it grants |
|---|---|
| `AmazonS3FullAccess` | Permission to read and write to any S3 bucket in the account |
| `AWSLambdaBasicExecutionRole` | Permission to write execution logs to CloudWatch so errors are visible |

### Trust policy

The trust policy defines which service is allowed to assume this role. It is set to `lambda.amazonaws.com`, meaning only Lambda can use it.

```json
{
    "Effect": "Allow",
    "Principal": {
        "Service": "lambda.amazonaws.com"
    },
    "Action": "sts:AssumeRole"
}
```

### Production note

`AmazonS3FullAccess` grants access to all buckets in the account. In a production environment this would be replaced with a custom policy scoped to only the Statcast bucket and only the operations needed:

```json
{
    "Effect": "Allow",
    "Action": ["s3:PutObject", "s3:GetObject", "s3:ListBucket"],
    "Resource": "arn:aws:s3:::baseball-statcast-data-516769757298-ap-southeast-2-an/*"
}
```

---

## Step 3 — ECR Repository

**Service:** Amazon Elastic Container Registry (ECR)
**Repository name:** `statcast-pipeline`

### What ECR is

ECR is a private storage registry for Docker container images. Think of it as a uniform storage room — before the worker (Lambda) can do their job each day, they need to put on the right uniform. The uniform is a container image that contains Python, all required packages, and the script itself.

### What a container image is

A container image is a self-contained, pre-packaged bundle of everything needed to run an application — the operating system layer, the Python runtime, all installed packages, and the script itself. The advantage over a standard Lambda zip deployment is size — Lambda's zip deployment caps at 250MB uncompressed, which pybaseball and its scientific computing dependencies (scipy, numpy, scikit-learn) exceed. A container image supports up to 10GB.

---

## Step 4 — Docker Container

**Tool:** Docker (running inside AWS CloudShell)
**Image size:** ~2.1GB

### Why CloudShell instead of local Docker

Docker Desktop requires hardware virtualisation to be enabled in the machine's BIOS — a setting commonly disabled on corporate laptops by IT policy. AWS CloudShell is a browser-based terminal running inside AWS, with Docker pre-installed and already authenticated to the account. No local setup is required and no virtualisation is needed.

### Dockerfile

```dockerfile
FROM public.ecr.aws/lambda/python:3.11

ENV HOME=/tmp

RUN yum clean all && \
    yum update -y && \
    yum install -y gcc gcc-c++ && \
    pip install --upgrade pip && \
    pip install \
    pybaseball \
    pandas==2.2.2 \
    numpy==1.26.4 \
    pyarrow==15.0.2 \
    scipy==1.11.4 \
    scikit-learn==1.3.2 \
    requests \
    openpyxl \
    boto3

COPY lambda_function_statcast.py .

CMD ["lambda_function_statcast.lambda_handler"]
```

Line by line:
- `FROM` — starts from AWS's official Python 3.11 Lambda base image
- `ENV HOME=/tmp` — sets the home directory at OS level so pybaseball can write its cache. Lambda's file system is read-only everywhere except `/tmp`
- `RUN yum install gcc` — installs C compilers needed to build scipy from source
- `pip install` — installs all Python packages. scikit-learn is pinned to 1.3.2 because newer versions require GCC 9.3+ which the Amazon Linux 2 base image does not provide. requests is required for the MLB Stats API calls
- `COPY` — copies the script into the image
- `CMD` — tells Lambda which function to invoke

### Build and push commands

```bash
cd /home/cloudshell-user
aws ecr get-login-password --region ap-southeast-2 | docker login --username AWS --password-stdin 516769757298.dkr.ecr.ap-southeast-2.amazonaws.com
docker build -t statcast-pipeline .
docker tag statcast-pipeline:latest 516769757298.dkr.ecr.ap-southeast-2.amazonaws.com/statcast-pipeline:latest
docker push 516769757298.dkr.ecr.ap-southeast-2.amazonaws.com/statcast-pipeline:latest
```

Docker caches unchanged layers — only modified layers are rebuilt and re-uploaded on subsequent updates, which significantly reduces rebuild time. The ECR authentication token expires periodically — always re-authenticate before pushing.

### pybaseball home directory fix

```python
import os
import pathlib

os.makedirs("/tmp/.pybaseball/cache", exist_ok=True)
pathlib.Path.home = classmethod(lambda cls: pathlib.Path("/tmp"))
```

These two lines run before any pybaseball import. pybaseball's cache module calls `pathlib.Path.home()` at import time to create a cache directory. Lambda's home directory is read-only, causing an `OSError` on startup. Setting the `HOME` environment variable does not fix this because pybaseball reads home via `pathlib`, not via the `HOME` variable. Monkey-patching `pathlib.Path.home` to return `/tmp` before the import resolves it entirely.

---

## Step 5 — Lambda Function (Pipeline)

**Service:** AWS Lambda
**Function name:** `statcast-daily-pull`
**Runtime:** Container image
**IAM role:** `lambda-statcast-s3-role`

### What Lambda is

Lambda is a serverless compute service — it runs code without a server running 24/7. The function only exists while executing. For a daily pull that takes roughly 60–90 seconds, this is significantly cheaper than keeping an EC2 instance running around the clock.

### Configuration

| Setting | Value | Reason |
|---|---|---|
| Memory | 1536 MB | Loading a full season of raw parquet files into pandas requires significantly more memory than the default. 512MB caused out-of-memory errors when loading all raw files for aggregation |
| Timeout | 5 minutes | pybaseball can take 30–60 seconds to pull a single day's data from Baseball Savant |

### What the Lambda function does

Each invocation executes the full pipeline in four steps:

**Step 1 — Raw pull**
1. Calculates the target date as sysdate minus 1 day
2. Pulls all Statcast pitch-level data for that date via pybaseball
3. Assigns role (SP or RP) to each pitcher row
4. Calls the MLB Stats API to retrieve official Earned Runs, Wins, Losses, Saves, Holds, and Blown Saves per game
5. Saves raw parquet to S3 at `raw/game_date={date}/statcast_{date}.parquet`

**Step 2 — Aggregation**
6. Loads all raw files from S3 and deduplicates at pitch level
7. Aggregates to one row per pitcher per game
8. Rounds rate statistics to 3 decimal places and run value metrics to 4
9. Casts `game_date` to proper date type
10. Saves to `aggregated/pitcher/pitcher_game_agg_2026.parquet`

**Step 2b — Dimension table updates**
11. Rebuilds `dim_pitcher` and `dim_pitcher_team` from the full aggregated file
12. Appends today's games to `dim_game`
13. Appends today's date to `dim_date` if not already present
14. `dim_team` and `dim_pitch_type` are static and not touched by the daily run

**Step 3 — CY Young model**
15. Filters aggregated data to SP and regular season (`game_type == "R"`) only
16. Runs dual CY Young model (voter + talent) per league on season-to-date data
17. Saves daily snapshot to `cy_young/calc_date={date}/rankings.parquet`
18. Overwrites `cy_young/_cy_young_latest/rankings.parquet`

### Why sysdate minus 1 day

Baseball Savant publishes overnight Statcast data by approximately 4AM US Eastern Time. The pipeline fires at 6AM Eastern — two hours after typical data availability — providing a buffer for late-publishing nights. Pulling sysdate minus 1 rather than minus 2 ensures rankings are current as of yesterday's games when they appear each morning.

---

## Step 6 — EventBridge Schedule

**Service:** Amazon EventBridge Scheduler
**Schedule name:** `statcast-daily-trigger`
**Cron expression:** `0 20 * * ? *`
**Execution time zone:** `Asia/Kuala_Lumpur` (UTC+08:00)
**IAM role:** Auto-created — `Amazon_EventBridge_Scheduler_LAMBDA_61f1fe33b3`
**Status:** Enabled
**Flexible time window:** 5 minutes
**Action after completion:** NONE
**Created:** 22 May 2026

### What EventBridge is

EventBridge is the alarm clock in the pipeline. It fires on a schedule and invokes a target — in this case the Lambda function. Without EventBridge, Lambda only runs when manually triggered.

### Cron expression breakdown

`0 20 * * ? *` in the `Asia/Kuala_Lumpur` execution timezone means: at minute 0, hour 20 (8PM KL = 10PM Sydney = 12:00 UTC = 6AM US Eastern), every day, every month, any day of week, any year.

EventBridge Scheduler interprets the cron hour in the specified execution timezone rather than UTC. 8PM KL (UTC+8) = 12:00 UTC = 6AM Eastern, which is two hours after Baseball Savant's typical 4AM Eastern data publication.

### Flexible time window

A 5-minute flexible time window is configured, meaning EventBridge may invoke Lambda at any point within 5 minutes of the scheduled time. This distributes load across AWS infrastructure and is appropriate for a non-latency-sensitive daily pipeline.

### Action after completion: NONE

Controls what EventBridge does to the schedule itself after it fires — not what happens to Lambda. NONE means the schedule persists and fires again the next day. DELETE would remove the schedule after a single invocation and is only appropriate for one-off tasks.

---

## Step 7 — AWS Glue and Athena

**Service:** AWS Glue (Data Catalog + Crawlers) and Amazon Athena

### What Glue is

Glue is a metadata catalogue for S3. It reads the parquet files in S3, infers their schema (column names and data types), and registers that schema as a table definition. It does not move or transform data — it simply tells Athena what the data looks like so SQL can be run against it.

A crawler is Glue's automated schema discovery tool. It scans an S3 path, inspects the files, and creates or updates table definitions in the catalogue. Crawlers are run after any structural change to the data — new columns, new partitions, or path changes.

### What Athena is

Athena is a serverless SQL query engine that reads directly from S3. There is no database server to manage — queries run on demand and are billed per terabyte of data scanned. For a pipeline of this scale, query costs are negligible.

### Crawler configuration

Each crawler is pointed at a specific S3 prefix. Isolation between prefixes is critical — if two files with different schemas exist under the same prefix, Glue will attempt to union them into a single table and either fail or produce corrupted column definitions.

| Crawler | S3 path | Exclusions |
|---|---|---|
| `statcast-crawler-raw` | `s3://.../raw/` | none |
| `statcast-crawler-aggregated-pitcher` | `s3://.../aggregated/pitcher/` | none |
| `statcast-crawler-dim` | `s3://.../dim/` | none |
| `statcast-crawler-cy-historical` | `s3://.../cy_young/` | `_cy_young_latest/**` |
| `statcast-crawler-cy-latest` | `s3://.../cy_young/_cy_young_latest/` | none |

The `_cy_young_latest/**` exclusion on the historical crawler prevents it from reading the latest file alongside the partitioned snapshots, which would cause a schema conflict since the latest file has no `calc_date` column.

### Partition index issue

Glue automatically creates partition indexes on crawled tables. For the raw table, the `game_date` column exists both as a Hive partition key (inferred from folder name `game_date=YYYY-MM-DD/`) and as a regular data column inside the parquet file, causing Athena to throw a duplicate column error. The fix is to delete the partition index from the table in the Glue console, and to disable automatic partition index creation in the crawler's output configuration.

---

## Data Pipeline — Raw Layer

### Source

Data is pulled from Baseball Savant via the `pybaseball` Python library, which scrapes the Statcast search endpoint. Statcast is MLB's pitch-tracking system, using radar and optical sensors installed in every stadium to capture ball and player movement data at the pitch level.

### Role assignment

All pitchers are captured in the raw layer — not just starters. Each pitch row is labelled with a `role` column:

- `SP` — the first pitcher to appear per `game_pk` / `inning_topbot` combination. In baseball, `inning_topbot` tracks which team is batting (`Top` = away team batting, `Bot` = home team batting), so each team has exactly one starting pitcher per game
- `RP` — any subsequent pitcher for the same team in the same game

### Official stats enrichment

The MLB Stats API (`statsapi.mlb.com`) is called separately for each game date. For each game, the boxscore endpoint returns official pitcher statistics as recorded by the official scorer. These are merged onto the Statcast data by `game_pk` and `pitcher` MLBAM ID.

Official stats captured:

| Column | What it means |
|---|---|
| `official_ER` | Earned Runs — runs that score without the aid of a defensive error. The basis for ERA calculation |
| `official_W` | Win — awarded to the pitcher of record when their team takes a lead it never relinquishes |
| `official_L` | Loss — awarded to the pitcher who surrenders the lead that results in a defeat |
| `official_SV` | Save — awarded to a reliever who finishes a game with a lead of 3 runs or fewer, or enters with the tying run on base |
| `official_HLD` | Hold — awarded to a reliever who enters in a save situation and exits without surrendering the lead, without recording a save |
| `official_BS` | Blown Save — awarded when a pitcher enters in a save situation and surrenders the lead |

Statcast does not record official ER directly — it only contains pitch-level run expectancy deltas (RE24), which is a continuous approximation. Official ER from the API is therefore essential for accurate ERA calculations.

### Raw columns retained (77 total)

70 Statcast columns + `role` + 6 official stats columns. Columns dropped from the full 118-column Statcast schema include deprecated fields (spin_dir, tfs_deprecated), batter-specific tracking (bat_speed, attack_angle), fielder IDs (fielder_2 through fielder_9), and hit coordinates (hc_x, hc_y).

**Identity and game context**

| Column | Full name | What it means |
|---|---|---|
| `game_pk` | Game Primary Key | A unique number identifying each MLB game. Used to link all pitches from the same game |
| `game_date` | Game Date | The calendar date of the game |
| `game_year` | Game Year | The season year — used to filter to the current season |
| `game_type` | Game Type | R = Regular season, P = Postseason, S = Spring training. Only R games count toward season statistics and the CY Young model |
| `pitcher` | Pitcher MLBAM ID | A permanent numeric ID assigned by MLB Advanced Media to each player. The key used to link data across all sources |
| `player_name` | Player Name | Display name of the pitcher |
| `p_throws` | Pitcher Throws | Handedness — R (right-handed) or L (left-handed) |
| `home_team` | Home Team | Three-letter team code for the home team |
| `away_team` | Away Team | Three-letter team code for the away team |
| `inning` | Inning | The current inning number (1–9+) |
| `inning_topbot` | Inning Half | Top = away team is batting, Bot = home team is batting. Used to determine which team the pitcher is pitching for |
| `at_bat_number` | At-Bat Number | A sequential counter for each at-bat within the game. Used to order events correctly |
| `pitch_number` | Pitch Number | Sequential count of pitches within the current at-bat |
| `n_thruorder_pitcher` | Times Through Order | How many times this pitcher has already faced the batting lineup. Pitcher effectiveness typically drops the third time through — hitters adjust |
| `n_priorpa_thisgame_player_at_bat` | Prior Plate Appearances This Game | How many times this specific batter has already faced this specific pitcher in the game |
| `pitcher_days_since_prev_game` | Days Since Previous Game | How many days of rest since the pitcher's last appearance |
| `pitcher_days_until_next_game` | Days Until Next Game | Days until the pitcher's next scheduled appearance |
| `age_pit` | Pitcher Age | Age of the pitcher at time of the game |
| `role` | Pitcher Role | SP (starting pitcher) or RP (relief pitcher) — derived during the raw pull |

**Count and base state**

| Column | Full name | What it means |
|---|---|---|
| `balls` | Ball Count | Current ball count in the at-bat (0–3). Four balls = walk |
| `strikes` | Strike Count | Current strike count (0–2). Three strikes = strikeout |
| `outs_when_up` | Outs When Up | Number of outs recorded before this at-bat began (0–2) |
| `on_1b` | Runner on First Base | MLBAM player ID of the runner on first base, or null if the base is empty |
| `on_2b` | Runner on Second Base | MLBAM player ID of the runner on second base, or null if empty |
| `on_3b` | Runner on Third Base | MLBAM player ID of the runner on third base, or null if empty |
| `stand` | Batter Stands | Handedness of the batter — R or L. Used for platoon analysis (how pitchers perform against same-handed vs opposite-handed batters) |

**Pitch arsenal and mechanics**

| Column | Full name | What it means |
|---|---|---|
| `pitch_type` | Pitch Type Code | Short code for the type of pitch: FF = Four-seam fastball, SI = Sinker, FC = Cutter, SL = Slider, CU = Curveball, CH = Changeup, FS = Split-finger, ST = Sweeper, KC = Knuckle curve |
| `pitch_name` | Pitch Name | Full descriptive name of the pitch type |
| `release_speed` | Release Speed | How fast the ball was thrown at the moment of release, in miles per hour |
| `effective_speed` | Effective Speed | Perceived speed from the batter's perspective, adjusted for how close to the plate the pitcher released the ball. A pitcher who releases 2 feet closer to the plate effectively appears to throw 2–3 mph faster |
| `release_spin_rate` | Release Spin Rate | How fast the ball is spinning at release, measured in revolutions per minute (RPM). Higher spin generally produces more movement and is harder to hit |
| `spin_axis` | Spin Axis | The direction of the spin axis in degrees (0–360). Determines which direction the ball breaks — topspin, backspin, sidespin |
| `release_extension` | Release Extension | How far in front of the pitching rubber the pitcher releases the ball, in feet. Greater extension means less reaction time for the batter |
| `release_pos_x` | Release Position X | Horizontal release point in feet from the centre of the rubber — left or right |
| `release_pos_z` | Release Position Z | Vertical release point in feet above the ground |
| `release_pos_y` | Release Position Y | Distance from home plate at the point of release |
| `pfx_x` | Horizontal Pitch Movement | How much the pitch moved horizontally compared to a theoretical spinless pitch, in inches |
| `pfx_z` | Vertical Pitch Movement | How much the pitch moved vertically compared to a theoretical spinless pitch, in inches |
| `api_break_z_with_gravity` | Vertical Break with Gravity | Total vertical drop of the pitch from release to plate, including the effect of gravity |
| `api_break_x_arm` | Arm-Side Horizontal Break | Horizontal movement from the pitcher's arm-side perspective |
| `api_break_x_batter_in` | Batter-In Horizontal Break | Horizontal movement toward or away from the batter |
| `arm_angle` | Arm Angle | The angle of the pitcher's arm at release in degrees. Affects both pitch shape and how deceptive the delivery is to batters |
| `plate_x` | Plate X Position | Where the pitch crossed home plate horizontally, in feet from the centre of the plate. Negative = inside to a right-handed batter |
| `plate_z` | Plate Z Position | Where the pitch crossed home plate vertically, in feet above the ground |
| `sz_top` | Strike Zone Top | The top of the batter's strike zone in feet. Varies by batter height and stance |
| `sz_bot` | Strike Zone Bottom | The bottom of the batter's strike zone in feet |
| `zone` | Zone | Which zone the pitch crossed the plate in: 1–9 = inside the strike zone, 11–14 = outside the strike zone |

**Outcomes**

| Column | Full name | What it means |
|---|---|---|
| `type` | Pitch Result Type | S = Strike (called or swinging), B = Ball, X = Ball put in play |
| `description` | Pitch Description | Detailed outcome: called_strike, swinging_strike, foul, ball, hit_into_play, blocked_ball, etc. |
| `events` | At-Bat Event | The final outcome of the at-bat — only populated on the last pitch of each at-bat. Values include: strikeout, walk, single, double, triple, home_run, field_out, grounded_into_dp, sac_fly, hit_by_pitch, etc. Null for non-terminal pitches |
| `bb_type` | Batted Ball Type | The trajectory of the batted ball: ground_ball, fly_ball, line_drive, or popup. Null for pitches not put in play |

**Batted ball quality**

| Column | Full name | What it means |
|---|---|---|
| `launch_speed` | Exit Velocity | How fast the ball came off the bat in miles per hour. A key indicator of contact quality — harder contact is harder to defend and more likely to result in extra-base hits. League average is roughly 88 mph; 95+ mph is considered "hard hit" |
| `launch_angle` | Launch Angle | The vertical angle of the ball off the bat in degrees. Negative = ground ball, 0–10° = low line drive, 10–25° = line drive, 25–50° = fly ball, 50°+ = popup |
| `hit_distance_sc` | Hit Distance | Estimated distance the batted ball travelled, in feet |
| `launch_speed_angle` | Launch Speed-Angle Category | A classification from 1–6 combining exit velocity and launch angle. 6 = Barrel (see below) |
| `estimated_ba_using_speedangle` | Expected Batting Average (xBA) | The probability of a hit based purely on exit velocity and launch angle, ignoring where the fielders were positioned. Ranges from 0 to 1 — an xBA of .800 means 80% of balls hit with that velocity and angle result in hits |
| `estimated_woba_using_speedangle` | Expected Weighted On-Base Average (xwOBA) | The expected run value of a batted ball based on exit velocity and launch angle. wOBA (Weighted On-Base Average) weights each outcome by how many runs it's worth — a home run counts more than a single. xwOBA measures the quality of contact independent of whether fielders happened to make plays. League average is typically around .320 |
| `woba_value` | Weighted On-Base Average Value | The actual wOBA value of the at-bat outcome. Walk ≈ 0.69, single ≈ 0.89, double ≈ 1.27, triple ≈ 1.62, home run ≈ 2.10, out = 0. A pitcher's wOBA-against measures how much offensive value their opposition actually generated |
| `woba_denom` | wOBA Denominator | Whether this plate appearance counts in the wOBA denominator (1 = yes, 0 = no). Intentional walks and sacrifice bunts are excluded |
| `babip_value` | BABIP Value | Whether this at-bat contributed to BABIP (Batting Average on Balls In Play). BABIP = hits on balls in play / balls in play, excluding strikeouts and home runs. League average is ~.300. Pitchers have limited control over BABIP — it's heavily influenced by defence and luck. Very low BABIP often indicates a pitcher has been lucky or has elite defensive support |
| `iso_value` | Isolated Power Value | Extra bases beyond a single — measures raw power. A single = 0, double = 1, triple = 2, home run = 3 |

**Run value**

| Column | Full name | What it means |
|---|---|---|
| `delta_run_exp` | Delta Run Expectancy | How much the run expectancy changed on each pitch, based on all 24 possible combinations of runners on base (empty/1st/2nd/3rd/1st+2nd/etc.) and outs (0/1/2). A strikeout with the bases loaded and no outs reduces run expectancy significantly — the pitcher gets credit. This is called RE24 and is one of the most granular measures of pitcher contribution |
| `delta_pitcher_run_exp` | Delta Pitcher Run Expectancy | The pitcher's personal share of the run expectancy change — adjusts for cases where a fielder made or missed a play that contributed to the outcome |

**Score state**

| Column | Full name | What it means |
|---|---|---|
| `home_score` | Home Score | The home team's score at the start of the at-bat |
| `away_score` | Away Score | The away team's score at the start of the at-bat |
| `bat_score` | Batting Team Score | Score of whichever team is currently batting |
| `fld_score` | Fielding Team Score | Score of whichever team is currently pitching/fielding |
| `post_home_score` | Post-Play Home Score | Home team score after the play resolved |
| `post_away_score` | Post-Play Away Score | Away team score after the play resolved |
| `post_bat_score` | Post-Play Batting Team Score | Batting team score after the play resolved |
| `post_fld_score` | Post-Play Fielding Team Score | Fielding team score after the play resolved |

---

## Data Pipeline — Aggregation Layer

The aggregation script reads all raw parquet files from S3, deduplicates at the pitch level, and rolls up to one row per pitcher per game. The result is a box score for every pitcher appearance in the season — both starters and relievers — in a single file with a `role` column.

All rate statistics are recalculated at the correct level of aggregation. Averaging game-level rate stats across games produces incorrect season totals — counting stats must be summed first and rates derived from those sums afterwards.

### Deduplication

Pitch-level deduplication is applied on `game_pk`, `pitcher`, `at_bat_number`, and `pitch_number`. This handles cases where the same game date was pulled multiple times (e.g. Lambda test runs or backfill reruns).

### Innings Pitched calculation

IP is calculated from outs recorded rather than maximum inning reached. Maximum inning overcounts IP for pitchers pulled mid-inning — a starter who records one out in the fifth inning before being removed would be credited 5.0 IP instead of the correct 4.1 (4.33 for ERA purposes). Outs are counted per play using the `events` column:

- Strikeout, force out, fly out, etc. = 1 out
- Grounded into double play, double play = 2 outs
- Triple play = 3 outs

`IP = total outs / 3`

IP is intentionally not rounded in the aggregated file. The CY Young model uses IP in ERA, FIP, and WHIP calculations, and rounding 6.33 to 6.3 would introduce small errors that compound across a full season.

### League mapping

League (AL or NL) is derived from `pitcher_team` — the team the pitcher is pitching for — not from `home_team` or `away_team`. Deriving it from home/away team caused NL pitchers to be misclassified as AL in interleague games. Correct Statcast team abbreviations for 2026:

**American League:** NYY, BOS, TOR, BAL, TB, CLE, MIN, CWS, KC, DET, HOU, ATH, SEA, LAA, TEX

**National League:** NYM, ATL, PHI, MIA, WSH, CHC, MIL, STL, PIT, CIN, LAD, SF, AZ, COL, SD

Note: Several abbreviations differ from common usage — TB (not TBR), CWS (not CHW), KC (not KCR), WSH (not WSN), SF (not SFG), SD (not SDP), AZ (not ARI), ATH (not OAK — the A's relocated to Las Vegas for 2026).

### Inherited runners (RP-specific)

When a relief pitcher enters with runners already on base, those runners are called "inherited runners." The reliever did not put them on base but is responsible for preventing them from scoring.

For each RP appearance, the entry pitch is identified and the runners on base at that moment are counted. Inherited runners scored is estimated as: runs scored in the same inning after the RP entered, capped at the number of inherited runners. This is an approximation — it can overcount in rare cases where the RP also allowed their own runners to score in the same inning.

### Rounding

Rate statistics are rounded before writing to S3 to keep the aggregated file clean and consistent with what is displayed in the API. IP, K_BB_ratio, and `wOBA_TBF_product` are deliberately left unrounded — IP is used in downstream calculations where precision matters, K_BB_ratio is a derived ratio that loses meaningful precision when rounded, and `wOBA_TBF_product` is an intermediate column used to recalculate weighted opponent wOBA at the season level and must stay at full precision.

| Precision | Columns |
|---|---|
| 3 decimal places | ERA, ERA_re24, FIP, WHIP, FIP_ERA_gap, xwOBA_against, wOBA_against, babip, LOB_pct, IR_stranding_pct, K_pct, BB_pct, swstr_pct, o_swing_pct, z_contact_pct, first_pitch_strike_pct, hard_hit_pct, barrel_pct, GB_pct, HR_per_9, opponent_wOBA |
| 4 decimal places | delta_run_exp |
| Unrounded | IP, K_BB_ratio, wOBA_TBF_product |

### Aggregated columns

| Column | Full name | Calculation | Notes |
|---|---|---|---|
| `role` | Pitcher Role | From raw | SP or RP |
| `game_type` | Game Type | From raw | R = Regular season, P = Postseason, S = Spring training |
| `GS` | Game Started | 1 if role == SP, else 0 | Used by CY Young season aggregation to count starts |
| `pitches` | Total Pitches | Count of all pitch rows | Includes all pitch types |
| `TBF` | Total Batters Faced | Count of rows where `events` is not null | Counts completed plate appearances only |
| `K` | Strikeouts | Count of strikeout events | |
| `BB` | Walks | Count of walk and hit-by-pitch events | Includes hit by pitch |
| `HR` | Home Runs | Count of home run events | |
| `H` | Hits | Count of singles, doubles, triples, and home runs | |
| `SO` | Strikeouts (display) | Same as K | Separate column matching traditional box score convention |
| `GB` | Ground Balls | Count of ground ball batted balls | |
| `FB` | Fly Balls | Count of fly ball and popup batted balls | |
| `LD` | Line Drives | Count of line drive batted balls | |
| `official_ER` | Official Earned Runs | From MLB Stats API | Falls back to RE24 approximation if API unavailable |
| `official_W` | Official Win | 1 if win recorded, 0 otherwise | From MLB Stats API |
| `official_L` | Official Loss | 1 if loss recorded, 0 otherwise | From MLB Stats API |
| `official_SV` | Official Save | 1 if save recorded, 0 otherwise | From MLB Stats API — RP-relevant |
| `official_HLD` | Official Hold | 1 if hold recorded, 0 otherwise | From MLB Stats API — RP-relevant |
| `official_BS` | Official Blown Save | 1 if blown save recorded, 0 otherwise | From MLB Stats API — RP-relevant |
| `IP` | Innings Pitched | Total outs / 3 | Fractional (e.g. 6.33 = 6⅓ innings) |
| `ERA` | Earned Run Average | (official_ER / IP) × 9 | How many earned runs a pitcher allows per 9 innings. League average is typically ~4.00. An ERA below 3.00 is considered excellent. Lower is better |
| `ERA_re24` | ERA via RE24 | (ER_re24 / IP) × 9 | ERA calculated using RE24-based run values rather than official ER. Kept for model transparency and comparison |
| `FIP` | Fielding Independent Pitching | ((13×HR) + (3×BB) - (2×K)) / IP + 3.10 | A version of ERA that only counts what the pitcher directly controls — home runs, walks, and strikeouts — removing the effect of the defence behind them and sequencing luck. The constant (~3.10) normalises FIP to the same scale as ERA. A pitcher with a 3.20 FIP but 4.00 ERA has been unlucky or poorly defended |
| `WHIP` | Walks Plus Hits Per Inning Pitched | (BB + H) / IP | How many baserunners a pitcher allows per inning. A WHIP of 1.00 means exactly one baserunner per inning, which is excellent. League average is typically ~1.30 |
| `FIP_ERA_gap` | FIP minus ERA | FIP - ERA | Positive = ERA is better than FIP (pitcher has been helped by luck or defence). Negative = ERA is worse than FIP (pitcher has been hurt by bad luck or poor defence). A large gap in either direction suggests regression toward the other |
| `xwOBA_against` | Expected wOBA Against | Mean of xwOBA per batted ball | The quality of contact allowed based on exit velocity and launch angle, independent of whether fielders made plays. A pitcher allowing consistent weak contact will outperform their ERA over time. Lower is better |
| `wOBA_against` | Actual wOBA Against | Mean of actual wOBA values | The offensive value actually generated against this pitcher. Compared to xwOBA to assess luck and defensive contribution |
| `babip` | Batting Average on Balls In Play | Mean of BABIP values | How often balls in play against this pitcher become hits. League average ~.300. Significantly below .300 may indicate luck or elite defence behind the pitcher |
| `hard_hit_pct` | Hard Hit Percentage | Balls with exit velo ≥ 95 mph / balls in play | The percentage of batted balls that were hit at 95 mph or harder. Hard contact is significantly more likely to result in hits and extra-base hits. Lower is better for the pitcher |
| `barrel_pct` | Barrel Percentage | Barrels / balls in play | A barrel is a batted ball with both optimal exit velocity (98+ mph) and optimal launch angle — the combination most likely to result in a home run or extra-base hit. The most dangerous contact type. Lower is better for the pitcher |
| `GB_pct` | Ground Ball Percentage | GB / (GB + FB + LD) | What percentage of batted balls are ground balls. Ground ball pitchers suppress home runs because ground balls cannot leave the park. Higher is generally better for the pitcher |
| `LOB_pct` | Left on Base Percentage | 1 - (official_ER / (H + BB - 1.4×HR)) | SP only, null for RP. The percentage of baserunners that the pitcher strands (prevents from scoring). League average is ~72%. Consistently high LOB% can indicate strong clutch pitching, or it can reflect luck that will regress |
| `K_pct` | Strikeout Percentage | K / TBF | The proportion of batters faced who strike out. More stable and predictive than raw strikeout totals because it accounts for the number of batters faced. Elite starters typically exceed 25% |
| `BB_pct` | Walk Percentage | BB / TBF | The proportion of batters faced who walk. The most controllable pitching skill. Elite command is typically below 7% |
| `K_BB_ratio` | Strikeout-to-Walk Ratio | K / (BB + 0.001) | How many strikeouts a pitcher records for each walk allowed. A ratio above 3.0 is considered excellent. The small constant (0.001) prevents division by zero for pitchers who have not allowed a walk |
| `swstr_pct` | Swinging Strike Percentage | Swinging strikes / total pitches | The percentage of all pitches that result in a swing-and-miss. The single best pitch-level predictor of future strikeout rate — a pitcher with high swSTR% will continue to miss bats even if their strikeout numbers temporarily dip |
| `o_swing_pct` | Chase Rate | Outside zone swings / outside zone pitches | How often batters swing at pitches outside the strike zone. A high chase rate means batters are fooled by the pitcher's stuff or command, generating weak contact and strikeouts. Higher is better for the pitcher |
| `z_contact_pct` | Zone Contact Percentage | Zone contacts / zone swings | How often batters make contact on pitches inside the strike zone. Lower is better for the pitcher — it means the pitcher is generating swing-and-miss even on pitches the batter should be able to handle |
| `first_pitch_strike_pct` | First Pitch Strike Percentage | First pitch strikes / total first pitches | Getting ahead 0-1 in the count is strongly associated with better outcomes for pitchers. First pitch strike rate is a foundational command metric |
| `delta_run_exp` | Cumulative Run Expectancy Delta | Sum of `delta_pitcher_run_exp` | The pitcher's total run value contribution across all pitches in the game, measured in runs above or below average using RE24. More negative = better. A -2.0 means the pitcher saved ~2 runs compared to average |
| `HR_per_9` | Home Runs Per Nine Innings | (HR / IP) × 9 | Rate of home runs allowed per 9 innings |
| `opponent_wOBA` | Opponent wOBA (Strength of Schedule) | TBF-weighted average of opponent team wOBA | Measures the quality of lineups the pitcher has faced. A pitcher with a .340 opponent wOBA has faced a harder schedule than one with .280. Weighted by TBF so a 7-inning start counts more than a 1-inning appearance |
| `wOBA_TBF_product` | wOBA × TBF | opponent_wOBA × TBF | Intermediate column used to correctly recalculate TBF-weighted opponent wOBA at the season level |
| `inherited_runners` | Inherited Runners | Runners on base at RP entry | RP only, null for SP. How many runners were on base when the reliever entered the game |
| `inherited_runners_scored` | Inherited Runners Scored | Estimated runs scored from inherited runners | RP only, null for SP. Approximation: runs scored in the entry inning after RP entry, capped at inherited_runners count |
| `IR_stranding_pct` | Inherited Runner Stranding Percentage | 1 - (inherited_runners_scored / inherited_runners) | RP only, null for SP and RP with no inherited runners. The percentage of inherited runners the reliever prevented from scoring. The RP equivalent of LOB% for starters. League average ~72% |

---

## Data Pipeline — Dimension Tables

Six dimension tables are maintained in the `dim/` folder. Each sits in its own subfolder to ensure Glue crawls them independently and creates separate, clean table definitions in Athena. If two files with different schemas shared the same prefix, Glue would attempt to union them and fail.

`dim_pitcher` and `dim_pitcher_team` are rebuilt daily by the Lambda. `dim_game` and `dim_date` are incrementally appended. `dim_team` and `dim_pitch_type` are static for the season and are only rebuilt by running `backfill_dim.py` manually if needed.

**`dim_pitcher`** — one row per pitcher. Columns: `pitcher` (MLBAM ID), `player_name`, `p_throws`. Updated daily from aggregated layer. Named `dim_pitcher` rather than `dim_player` to reflect that the database currently covers pitchers only — when batter data is added, a separate `dim_batter` table will be created.

**`dim_pitcher_team`** — one row per player per team stint. Tracks trades and DFA/signings throughout the season. Columns: `pitcher`, `player_name`, `team`, `team_start_date`, `team_end_date`. A null `team_end_date` indicates the pitcher's current team. When a pitcher is traded, the old stint is closed with a `team_end_date` and a new row is inserted. This is a Slowly Changing Dimension Type 2 (SCD2) pattern — it preserves the full history of team affiliations rather than overwriting the current team. The API summary endpoint joins on this table filtering `team_end_date IS NULL` to return current team only.

**`dim_team`** — hardcoded reference table. One row per team. Columns: `team`, `full_name`, `league`, `division`. Static for the season. Note: ATH = Athletics (formerly Oakland, relocated to Las Vegas for 2026).

**`dim_date`** — one row per game date. Columns: `game_date`, `day_of_week`, `day_of_week_num`, `week_number`, `month`, `month_name`, `season_segment`, `days_into_season`. Season segments: Spring (March/April), Summer (May/June), Late Summer (July/August), Pennant Race (September).

**`dim_game`** — one row per game. Columns: `game_pk`, `game_date`, `game_type`, `home_team`, `away_team`. `game_type` is derived directly from the raw Statcast data.

**`dim_pitch_type`** — one row per pitch type code. Columns: `pitch_type`, `pitch_name`, `pitch_category`. Categories: Fastball (FF, SI, FC, FA), Breaking (SL, CU, KC, ST, SV, CS), Offspeed (CH, FS, FO, SC), Other (KN, EP, PO, IN, UN).

---

## Data Pipeline — CY Young Model

### What the Cy Young Award is

The Cy Young Award is given annually to the best pitcher in each league (American League and National League) as voted on by the Baseball Writers' Association of America (BBWAA). Each voter submits a ranked ballot of their top five pitchers. Points are awarded based on placement — 7 for first, 4 for second, 3 for third, 2 for fourth, 1 for fifth — and the pitcher with the most total points wins.

Because the award is voted on by journalists rather than analysts, the result is shaped as much by perception, narrative, and traditional statistics as by underlying performance. A pitcher on a winning team with a shiny ERA and a high strikeout total will typically beat an analytically superior pitcher on a bad team — even if objective measures suggest the opposite.

### Scope

The model runs on starting pitchers (`role == "SP"`) in regular season games (`game_type == "R"`) only. Relievers and postseason games are explicitly excluded before the model runs.

### Design philosophy: dual model output

The model produces two independent scores and rankings for each pitcher:

**Voter model (`cy_score_voter`)** — predicts how BBWAA voters are likely to perceive the pitcher. Voters tend to prioritise traditional, results-based statistics — ERA and raw strikeout totals. This model intentionally incorporates voter bias. ERA is the headline number that appears on scoreboards and broadcasts; voters overwhelmingly defer to it. Strikeout totals are tracked as a season-long milestone race. WHIP is visible in box scores. The voter model is not trying to identify the best pitcher — it is trying to predict who writers will vote for.

**Talent model (`cy_score_talent`)** — predicts the pitcher's true underlying performance independent of luck, defence, and narrative. A pitcher with an ugly ERA but an elite FIP and xwOBA has been hurt by bad defence or sequencing — their true quality is better than their results. The talent model surfaces these pitchers. It uses only process metrics and defence-independent statistics that are robust to the noise that corrupts traditional results.

**Score delta (`score_delta`)** — `cy_score_talent` minus `cy_score_voter`. Positive = analytically elite but underrated by traditional metrics (likely being hurt by bad defence or poor run support). Negative = strong traditional numbers but weaker underlying process (ERA likely inflated by good luck or strong defence).

**Rank delta (`rank_delta`)** — `cy_rank_voter` minus `cy_rank_talent`. Positive = ranks higher in the voter model than the talent model, meaning traditional stats flatter the pitcher. Negative = ranks higher in the talent model than the voter model, meaning analytics see more value than traditional stats suggest.

This creates four meaningful quadrants:

- **High voter, high talent** — undisputed Cy Young contenders. Both models agree. These pitchers are excellent by any measure
- **High voter, low talent** — lucky or defence-aided. Shiny ERA but weak peripherals. Likely to regress as the season continues
- **Low voter, high talent** — analytically elite but unrecognised. Often pitching for bad teams with poor run support and weak defence. Undervalued by voters
- **Low voter, low talent** — neither traditional nor analytical standouts

### Dynamic IP threshold

The minimum innings pitched qualification threshold scales with season progress to prevent spot starters and openers from appearing in early-season rankings:

```
min_ip = 162 × (days_elapsed / 187 total days) × 0.85
```

The 0.85 multiplier provides a conservative buffer. The threshold is floored at 5 IP in the first days of the season to ensure enough pitchers qualify.

### Workload bonus

An additive bonus rewards pitchers with elite workload — a factor Cy Young voters consistently reward:

- IP ≥ 75% of the season-progress-adjusted full-season threshold: +0.03
- GS ≥ 30: +0.02

These are added after normalisation to shift the final score rather than being absorbed by scaling.

### Consistency score

FIP standard deviation across individual starts is calculated per pitcher. A pitcher who is consistently dominant across 30 starts is valued more highly than one with two brilliant outings and several poor ones. Included in output for reference but not currently a scoring feature.

### Voter model features and weights

Features are normalised using `RobustScaler` (scales by median and interquartile range rather than min-max) before weights are applied. RobustScaler is used because min-max scaling is sensitive to outliers — a single historically dominant season could compress all other pitchers into a narrow range. After scaling, features with negative weights are sign-flipped so lower-is-better metrics score correctly before the weighted sum.

| Feature | Full name | Weight | Direction | Why voters care |
|---|---|---|---|---|
| `IP` | Innings Pitched | 2.0 | Higher is better | Volume is the single most important factor in Cy Young voting. Voters have consistently penalised pitchers with fewer than ~200 IP even when their rate stats are elite. A pitcher who throws 180 innings with a 2.50 ERA will often lose to one who throws 220 innings with a 3.10 ERA |
| `ERA` | Earned Run Average | -2.0 | Lower is better | The traditional gold standard for voter evaluation. ERA appears on every broadcast, every box score, every newspaper. Voters overwhelmingly rely on it as the headline number — it answers "how many runs does this pitcher give up?" in a simple, intuitive way |
| `WHIP` | Walks Plus Hits Per Inning Pitched | -1.2 | Lower is better | The second most visible traditional stat in broadcast and print coverage. It tells voters how many baserunners a pitcher allows per inning — a clean, readable indicator of control and dominance |
| `SO` | Total Strikeouts | 1.5 | Higher is better | Voters track the race to 200 and 250 strikeouts as a milestone. Raw counting stats rather than rates are used here because voters respond to totals — "he struck out 280 batters" resonates more than "he struck out 28% of batters faced" |
| `FIP` | Fielding Independent Pitching | -0.8 | Lower is better | A small weight acknowledges analytically-minded voters who consult FIP, while keeping it subordinate to ERA. The majority of voters still default to ERA; FIP is a tiebreaker at best |

### Talent model features and weights

| Feature | Full name | Weight | Direction | Why it predicts true performance |
|---|---|---|---|---|
| `K_pct` | Strikeout Percentage | 1.2 | Higher is better | Strikeout rate is the most stable and predictive individual pitcher skill. Rate removes the confounding effect of innings pitched — a pitcher who strikes out 30% of batters faced is elite regardless of whether they threw 180 or 220 innings |
| `BB_pct` | Walk Percentage | -1.2 | Lower is better | Walk rate is the best measure of command. Walks are entirely the pitcher's responsibility — no defensive context, no luck. A pitcher who walks 4% of batters faced has significantly better command than one who walks 9% |
| `FIP` | Fielding Independent Pitching | -1.5 | Lower is better | Highest weight in the talent model. FIP synthesises the three true outcomes (strikeouts, walks, home runs) into a single ERA-scale number that removes defence and sequencing luck. A pitcher with a 2.80 FIP has been dominant regardless of what their ERA says |
| `xwOBA_against` | Expected wOBA Against | -1.5 | Lower is better | Jointly highest alongside FIP. Measures quality of contact allowed based on exit velocity and launch angle, independent of whether fielders made plays. A pitcher can allow a screaming line drive that an outfielder catches — ERA says 0 runs, xwOBA correctly records the dangerous contact |
| `swstr_pct` | Swinging Strike Percentage | 1.0 | Higher is better | Best pitch-level leading indicator of strikeout rate. A pitcher who generates lots of swing-and-miss will continue to do so — it reflects stuff quality rather than outcomes that fluctuate with sequencing and luck |
| `barrel_pct` | Barrel Percentage Against | -1.0 | Lower is better | Barrels are the most dangerous contact type — optimal exit velocity and launch angle for extra-base hits. Allowing fewer barrels indicates the pitcher is getting batters to make poor contact, not just that fielders happened to be in the right place |
| `delta_run_exp` | Cumulative Run Expectancy Delta | -1.2 | Lower is better | Season total RE24 — measures overall run prevention in a context-neutral way. Accounts for the leverage of each pitch situation: getting out of a bases-loaded jam is worth more than retiring the first batter of the inning. More negative = more value added |
| `opponent_wOBA` | Opponent wOBA (Strength of Schedule) | 0.8 | Higher is better | Rewards pitchers who face stronger lineups. A 2.90 ERA against the top offences in the league is more impressive than a 2.90 ERA against weak lineups. The weight is modest — schedule strength is a real but secondary factor |
| `IP` | Innings Pitched | 1.0 | Higher is better | Included at lower weight than voter model — accumulated value still matters. A pitcher who is dominant for 220 innings has contributed more to their team than one who is equally dominant for 160 innings |

---

## Step 8 — API Lambda Functions

Three Lambda functions serve API requests. All use Python 3.11 inline runtime with the `AWSSDKPandas-Python311` layer, which provides pandas and pyarrow without requiring a Docker container.

### `statcast-cy-latest-rank-api` — Full rankings

**Function name:** `statcast-cy-latest-rank-api`
**Memory:** 256 MB | **Timeout:** 30 seconds

Reads `cy_young/_cy_young_latest/rankings.parquet` from S3, sorts by `cy_rank_voter` ascending, and returns the complete rankings payload with all columns. Intended for downstream analytical use where all fields are needed.

Response format is controlled by a query parameter:
- Default (no parameter): JSON
- `?format=xlsx`: Excel workbook with `Content-Disposition` header for download

### `statcast-cy-summary-api` — Summary rankings

**Function name:** `statcast-cy-summary-api`
**Memory:** 256 MB | **Timeout:** 30 seconds

Reads the same latest rankings file and joins `dim_pitcher_team` to add a `current_team` column — filtering on `team_end_date IS NULL` ensures only the pitcher's current team is returned, not historical stints. This means if a pitcher was traded mid-season, the API correctly returns the new team only.

Returns a curated subset of columns rounded for readability. Rate stats are rounded to 3 decimal places; ERA, WHIP, FIP, FIP_ERA_gap, and IP are rounded to 2 decimal places. Model scores are rounded to 3 decimal places.

Columns returned: `player_name`, `current_team`, `league`, `p_throws`, `as_of_date`, `cy_rank_voter`, `cy_rank_talent`, `rank_delta`, `GS`, `IP`, `SO`, `official_W`, `official_L`, `ERA`, `WHIP`, `FIP`, `FIP_ERA_gap`, `xwOBA_against`, `hard_hit_pct`, `barrel_pct`, `GB_pct`, `K_pct`, `BB_pct`, `swstr_pct`, `cy_score_voter`, `cy_score_talent`, `score_delta`.

---

## Step 9 — API Gateway

**Service:** Amazon API Gateway (HTTP API)
**API name:** `statcast-cy-latest-api`
**Stage:** `$default` (auto-deploy enabled)
**Invoke URL:** `https://549maji0ad.execute-api.ap-southeast-2.amazonaws.com`

### What API Gateway is

API Gateway is the front door that sits between the outside world and a Lambda function. Without it, Lambda can only be triggered internally by AWS services such as EventBridge. API Gateway exposes a public HTTPS URL — any HTTP client (browser, Python script, curl) can call it directly.

### Routes

| Method | Path | Integration | Purpose |
|---|---|---|---|
| GET | `/cy-rankings` | `statcast-cy-latest-rank-api` | Full rankings, all columns |
| GET | `/cy-summary` | `statcast-cy-summary-api` | Curated summary with current team |

### Endpoints

```
https://549maji0ad.execute-api.ap-southeast-2.amazonaws.com/cy-rankings
https://549maji0ad.execute-api.ap-southeast-2.amazonaws.com/cy-rankings?format=xlsx
https://549maji0ad.execute-api.ap-southeast-2.amazonaws.com/cy-summary
https://549maji0ad.execute-api.ap-southeast-2.amazonaws.com/cy-summary?format=xlsx
```

### Cost

HTTP API Gateway: $1 per million requests. At personal usage volumes this is effectively free. The Lambda free tier covers 1 million invocations per month.

---

## Scripts

| Script | Purpose | Run location |
|---|---|---|
| `clean_slate.py` | Deletes all objects in the S3 bucket. Run before a full rebackfill. Note: also deletes raw/ — ensure raw backfill is re-run after using this script | Local |
| `backfill_statcast.py` | Pulls raw Statcast data day-by-day for a date range and saves to S3 | Local |
| `backfill_aggregate.py` | Reads all raw files, deduplicates, and builds the full season aggregated file at `aggregated/pitcher/` | Local |
| `backfill_dim.py` | Builds all six dimension tables from aggregated and raw data, writing each to its own subfolder under `dim/` | Local |
| `cy_young_backfill.py` | Runs the CY Young model for every date in the aggregated layer | Local |
| `lambda_function_statcast.py` | Daily pipeline — raw pull, aggregation, dim updates, CY Young model. Runs inside Lambda container | AWS Lambda |
| `lambda_function_cy_api.py` | Full rankings API — reads latest parquet, returns JSON or xlsx | AWS Lambda (inline) |
| `lambda_function_cy_summary_api.py` | Summary rankings API — curated columns, current team join, rounded stats, returns JSON or xlsx | AWS Lambda (inline) |

---

## Pipeline Status

| Component | Status | Notes |
|---|---|---|
| S3 bucket | Live | `baseball-statcast-data-516769757298-ap-southeast-2-an` |
| IAM role | Live | `lambda-statcast-s3-role` |
| ECR repository | Live | `statcast-pipeline` |
| Docker image | Live | ~2.1GB, scikit-learn==1.3.2, requests, openpyxl |
| Lambda — pipeline | Live | `statcast-daily-pull` — full pipeline, 1536MB, sysdate-1 |
| Lambda — full API | Live | `statcast-cy-latest-rank-api` — returns JSON or xlsx |
| Lambda — summary API | Live | `statcast-cy-summary-api` — returns JSON or xlsx, current team join |
| EventBridge schedule | Live | `0 20 * * ? *` Asia/Kuala_Lumpur — 8PM KL / 10PM Sydney / 6AM Eastern |
| API Gateway | Live | `statcast-cy-latest-api` — GET /cy-rankings and GET /cy-summary |
| Glue crawlers | Live | 5 crawlers — raw, aggregated-pitcher, dim, cy-historical, cy-latest |
| Athena | Live | statcast-db — all tables queryable |
| Raw backfill | Complete | March 25 – May 24 2026, all pitchers, role column, expanded official stats |
| Aggregation | Complete | Combined SP+RP, rounding applied, game_date as date type |
| Dimension tables | Complete | All six dims in subfolders, daily Lambda updates for pitcher/pitcher_team/game/date |
| CY Young backfill | Complete | 61 game days, dual model, AL and NL, SP + regular season filter |
| Latest rankings | Live | `cy_young/_cy_young_latest/rankings.parquet` current as of May 24 2026 |

---

## Next Steps

| Step | Tool | Purpose |
|---|---|---|
| GitHub repository | Git + GitHub | Version control for all scripts |

---

## Changes

### 23 May 2026

Complete rebuild of the pipeline following expansion from a CY Young-only starter model to a modular pitching database covering all pitchers.

**Raw layer**

The raw layer previously captured starting pitchers only. It now captures all pitchers with a `role` column (`SP` or `RP`) distinguishing starters from relievers. Official stats expanded from W/L/ER to include saves (`official_SV`), holds (`official_HLD`), and blown saves (`official_BS`). S3 paths updated to Hive-style partitioning (`raw/game_date=*/`) for Athena compatibility. File naming updated from `statcastSP_*.parquet` to `statcast_*.parquet`. Total columns: 77 (up from 73).

**Aggregation layer**

Single combined file `pitcher_game_agg_2026.parquet` covering both SP and RP. Key additions: `role`, `game_type`, `GS` flag, `official_SV`, `official_HLD`, `official_BS`, `inherited_runners`, `inherited_runners_scored`, `IR_stranding_pct`. `LOB_pct` is now SP-only (null for RP rows). `game_type` added to groupby so the field flows through to dimension tables.

**Dimension tables**

Six new dimension tables added to `dim/`: `dim_player`, `dim_player_team`, `dim_team`, `dim_date`, `dim_game`, `dim_pitch_type`. `dim_player_team` implements SCD Type 2 tracking for mid-season trades — one row per player per team stint, `team_end_date` null = current team.

**CY Young model**

Explicit `role == "SP"` and `game_type == "R"` filters added before the model runs. Without these, RP rows and postseason data could contaminate rankings. CY Young S3 paths updated to Hive-style (`cy_young/calc_date=*/rankings.parquet`).

**Lambda and infrastructure**

Memory increased from 512MB to 1536MB — loading a full season of raw files into pandas for aggregation exceeds 512MB. Sysdate offset changed from -2 to -1 days, aligned with EventBridge schedule moved to 6AM Eastern (2-hour buffer after 4AM Eastern data publication). Dockerfile updated: `scikit-learn` pinned to 1.3.2 (newer versions require GCC 9.3+, incompatible with the Amazon Linux 2 base image), `requests` added (was missing from original Dockerfile). EventBridge execution timezone corrected to `Asia/Kuala_Lumpur` (UTC+8).

**Architecture**

Data architecture formalised around medallion principles: `raw/` = bronze, `aggregated/` and `dim/` = silver, `cy_young/` = gold. Hive-style partitioning adopted on raw and CY Young layers.

**New scripts**

`clean_slate.py`, `backfill_aggregate.py`, `backfill_dim.py` added. `backfill_statcast.py` and `cy_young_backfill.py` updated for new schema.

---

### 25 May 2026

Major infrastructure overhaul covering S3 restructuring, Athena/Glue setup, dim table rename, API expansion, and schema quality fixes.

**S3 restructuring**

The `aggregated/` folder was split into `aggregated/pitcher/` to accommodate future expansion. When a batter aggregation module is added, it will sit at `aggregated/batter/` with its own crawler and Athena table — no schema conflicts, no restructuring required at that point. This is a forward-compatibility change with no impact on current functionality.

Dimension tables moved from a flat `dim/` layout to isolated subfolders (`dim/dim_pitcher/`, `dim/dim_pitcher_team/`, etc.). The flat layout caused Glue to attempt unioning all six dim files into one table when the crawler scanned the `dim/` prefix, producing a corrupted schema. Subfolder isolation ensures each dim gets its own Athena table with a clean, correct schema.

The latest CY Young rankings file moved from a flat `cy_young/_cy_rankings_latest.parquet` to `cy_young/_cy_young_latest/rankings.parquet`. The folder approach allows the Glue crawler for the latest table to be pointed directly at `_cy_young_latest/` — Glue's default exclusion of `_`-prefixed paths was causing the file to be skipped when the historical crawler scanned the parent `cy_young/` prefix.

**Dimension table rename**

`dim_player` → `dim_pitcher` and `dim_player_team` → `dim_pitcher_team`. The original names implied general player coverage that the database does not yet have. The pitcher-specific naming makes the scope explicit and leaves room for a future `dim_batter` and `dim_batter_team` without ambiguity.

**Lambda — dim update step added**

The Lambda previously had no mechanism to keep dimension tables current after the initial backfill. A new Step 2b runs after aggregation each day and rebuilds `dim_pitcher` and `dim_pitcher_team` from the full aggregated file, appends new games to `dim_game`, and appends the current date to `dim_date`. `dim_team` and `dim_pitch_type` are static and remain unchanged. This ensures trade tracking (`dim_pitcher_team`) stays accurate throughout the season without manual intervention — if a pitcher changes teams, the old stint is closed and a new row is opened automatically.

**Lambda — schema fixes**

`game_date` is now cast to a proper Python `date` type (`.dt.date`) before writing the aggregated parquet. Previously it was written as a timestamp (microseconds since epoch), which Glue inferred as `bigint`, requiring a cast in every Athena query. The fix writes a true `date` column, allowing direct equality joins in SQL without any conversion.

A rounding block was added before the aggregated parquet write. Rate statistics are rounded to 3 decimal places; run value metrics to 4. IP, `K_BB_ratio`, and `wOBA_TBF_product` are left unrounded — IP is used in downstream calculations where precision matters, `wOBA_TBF_product` is an intermediate column whose rounding would corrupt the weighted average recalculation in the CY model.

A `from datetime import timedelta` statement inside the dim update step was shadowing the top-level `timedelta` import, causing an `UnboundLocalError` on every invocation. The duplicate import was removed.

**Glue and Athena**

Five Glue crawlers created and configured: `statcast-crawler-raw`, `statcast-crawler-aggregated-pitcher`, `statcast-crawler-dim`, `statcast-crawler-cy-historical`, `statcast-crawler-cy-latest`. The historical crawler uses a `_cy_young_latest/**` exclusion pattern to prevent the latest folder from being catalogued twice. Automatic partition index creation was disabled on the raw crawler to prevent the duplicate `game_date` column error in Athena.

**API expansion**

The old API (`statcast-cy-api`) was deleted and recreated as `statcast-cy-latest-api` with two routes. The full rankings endpoint (`/cy-rankings`) returns all model columns as JSON or xlsx. A new summary endpoint (`/cy-summary`) returns a curated subset of columns rounded for readability, with a `current_team` column joined from `dim_pitcher_team` where `team_end_date IS NULL` — this ensures traded pitchers show their new team only. Both endpoints support `?format=xlsx` for direct spreadsheet download. The CSV format was replaced by xlsx to handle accented characters in player names (e.g. José Soriano, Cristopher Sánchez) without encoding corruption.

---

*Log current as of 25 May 2026*
