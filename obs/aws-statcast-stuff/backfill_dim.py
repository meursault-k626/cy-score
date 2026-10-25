import boto3
import pandas as pd
import pyarrow.parquet as pq
import io
import os
from datetime import date, timedelta

BUCKET = "baseball-statcast-data-516769757298-ap-southeast-2-an"
TEMP_DIR = r"C:\Users\NizamA\southside_temp"
SEASON_START = date(2026, 3, 25)
os.makedirs(TEMP_DIR, exist_ok=True)

s3 = boto3.client("s3")

def upload(df, filename, subfolder):
    local_path = os.path.join(TEMP_DIR, filename)
    df.to_parquet(local_path, index=False)
    s3_key = f"dim/{subfolder}/{filename}"
    s3.upload_file(local_path, BUCKET, s3_key)
    os.remove(local_path)
    print(f"[OK] {s3_key} — {len(df)} rows")


# Load aggregated (needed for player, player_team, game dims)
# Run backfill_aggregate.py before this script.

print("Loading aggregated layer...")
resp = s3.get_object(Bucket=BUCKET, Key="aggregated/pitcher/pitcher_game_agg_2026.parquet")
agg = pq.read_table(io.BytesIO(resp["Body"].read())).to_pandas()
agg["game_date"] = pd.to_datetime(agg["game_date"]).dt.date


# Load one raw file to derive pitch types
# (pitch_type/pitch_name schema is consistent across all dates)

print("Loading sample raw file for pitch type reference...")
paginator = s3.get_paginator("list_objects_v2")
raw_sample = None
for page in paginator.paginate(Bucket=BUCKET, Prefix="raw/"):
    for obj in page.get("Contents", []):
        if obj["Key"].endswith(".parquet"):
            resp_raw = s3.get_object(Bucket=BUCKET, Key=obj["Key"])
            raw_sample = pq.read_table(io.BytesIO(resp_raw["Body"].read())).to_pandas()
            break
    if raw_sample is not None:
        break


# dim_team — hardcoded reference table

print("\nBuilding dim_team...")
dim_team = pd.DataFrame([
    # American League East
    ("NYY", "New York Yankees", "AL", "AL East"),
    ("BOS", "Boston Red Sox", "AL", "AL East"),
    ("TOR", "Toronto Blue Jays", "AL", "AL East"),
    ("BAL", "Baltimore Orioles", "AL", "AL East"),
    ("TB", "Tampa Bay Rays", "AL", "AL East"),
    # American League Central
    ("CLE", "Cleveland Guardians", "AL", "AL Central"),
    ("MIN", "Minnesota Twins", "AL", "AL Central"),
    ("CWS", "Chicago White Sox", "AL", "AL Central"),
    ("KC", "Kansas City Royals", "AL", "AL Central"),
    ("DET", "Detroit Tigers", "AL", "AL Central"),
    # American League West
    ("HOU", "Houston Astros", "AL", "AL West"),
    ("ATH", "Athletics", "AL", "AL West"),
    ("SEA", "Seattle Mariners", "AL", "AL West"),
    ("LAA", "Los Angeles Angels", "AL", "AL West"),
    ("TEX", "Texas Rangers", "AL", "AL West"),
    # National League East
    ("NYM", "New York Mets", "NL", "NL East"),
    ("ATL", "Atlanta Braves", "NL", "NL East"),
    ("PHI", "Philadelphia Phillies", "NL", "NL East"),
    ("MIA", "Miami Marlins", "NL", "NL East"),
    ("WSH", "Washington Nationals", "NL", "NL East"),
    # National League Central
    ("CHC", "Chicago Cubs", "NL", "NL Central"),
    ("MIL", "Milwaukee Brewers", "NL", "NL Central"),
    ("STL", "St. Louis Cardinals", "NL", "NL Central"),
    ("PIT", "Pittsburgh Pirates", "NL", "NL Central"),
    ("CIN", "Cincinnati Reds", "NL", "NL Central"),
    # National League West
    ("LAD", "Los Angeles Dodgers", "NL", "NL West"),
    ("SF", "San Francisco Giants", "NL", "NL West"),
    ("AZ", "Arizona Diamondbacks", "NL", "NL West"),
    ("COL", "Colorado Rockies", "NL", "NL West"),
    ("SD", "San Diego Padres", "NL", "NL West"),
], columns=["team", "full_name", "league", "division"])

upload(dim_team, "dim_team.parquet", "dim_team")


# dim_pitch_type — derived from raw sample + category mapping

print("Building dim_pitch_type...")
pitch_category = {
    "FF": "Fastball", # 4-Seam Fastball
    "SI": "Fastball", # Sinker
    "FC": "Fastball", # Cutter
    "FA": "Fastball", # Generic Fastball
    "SL": "Breaking", # Slider
    "CU": "Breaking", # Curveball
    "KC": "Breaking", # Knuckle Curve
    "ST": "Breaking", # Sweeper
    "SV": "Breaking", # Slurve
    "CS": "Breaking", # Slow Curve
    "CH": "Offspeed", # Changeup
    "FS": "Offspeed", # Split-Finger
    "FO": "Offspeed", # Forkball
    "SC": "Offspeed", # Screwball
    "KN": "Other", # Knuckleball
    "EP": "Other", # Eephus
    "PO": "Other", # Pitchout
    "IN": "Other", # Intentional Ball
    "UN": "Other", # Unknown
}

if raw_sample is not None:
    dim_pitch_type = (
        raw_sample[["pitch_type", "pitch_name"]]
        .dropna(subset=["pitch_type"])
        .drop_duplicates()
        .sort_values("pitch_type")
        .reset_index(drop=True)
    )
    dim_pitch_type["pitch_category"] = dim_pitch_type["pitch_type"].map(pitch_category).fillna("Other")
else:
    # Fallback: build from the category map directly
    dim_pitch_type = pd.DataFrame([
        {"pitch_type": k, "pitch_name": k, "pitch_category": v}
        for k, v in pitch_category.items()
    ])

upload(dim_pitch_type, "dim_pitch_type.parquet", "dim_pitch_type")


# dim_player — one row per pitcher, most recent known attributes

print("Building dim_pitcher...")
dim_player = (
    agg.sort_values("game_date")
    .groupby("pitcher")
    .agg(
        player_name=("player_name", "last"),
        p_throws=("p_throws", "last"),
        age_pit=("p_throws", "count"),       # placeholder — use raw if available
    )
    .reset_index()
    .drop(columns=["age_pit"])
)

# Age: take from most recent game in aggregated
age_latest = (
    agg.sort_values("game_date")
    .groupby("pitcher")[["player_name"]]
    .last()
    .reset_index()
)

# If raw sample available, pull age_pit from it
if raw_sample is not None and "age_pit" in raw_sample.columns:
    age_df = (
        raw_sample[["pitcher", "age_pit"]]
        .dropna()
        .groupby("pitcher")["age_pit"]
        .last()
        .reset_index()
    )
    dim_player = dim_player.merge(age_df, on="pitcher", how="left")
else:
    dim_player["age_pit"] = None

upload(dim_player, "dim_pitcher.parquet", "dim_pitcher")


# dim_player_team — one row per player per team stint
# Tracks trades: team_end_date null = current team

print("Building dim_pitcher_team...")

player_games = (
    agg[["pitcher", "player_name", "game_date", "pitcher_team"]]
    .drop_duplicates(subset=["pitcher", "game_date", "pitcher_team"])
    .sort_values(["pitcher", "game_date"])
    .reset_index(drop=True)
)

stints = []
for pitcher_id, group in player_games.groupby("pitcher"):
    group = group.sort_values("game_date").reset_index(drop=True)
    current_team = None
    stint_start = None
    player_name = group["player_name"].iloc[0]

    for _, row in group.iterrows():
        if row["pitcher_team"] != current_team:
            if current_team is not None:
                # Close previous stint — end date is day before new team's first game
                stints.append({
                    "pitcher": pitcher_id,
                    "player_name": player_name,
                    "team": current_team,
                    "team_start_date": stint_start,
                    "team_end_date": row["game_date"] - timedelta(days=1),
                })
            current_team = row["pitcher_team"]
            stint_start = row["game_date"]

    # Final open stint — current team
    stints.append({
        "pitcher": pitcher_id,
        "player_name": player_name,
        "team": current_team,
        "team_start_date": stint_start,
        "team_end_date": None,
    })

dim_player_team = pd.DataFrame(stints)
upload(dim_player_team, "dim_pitcher_team.parquet", "dim_pitcher_team")


# dim_game — one row per game

print("Building dim_game...")
dim_game = (
    agg[["game_pk", "game_date", "game_type", "home_team", "away_team"]]
    .drop_duplicates(subset=["game_pk"])
    .sort_values("game_date")
    .reset_index(drop=True)
)

upload(dim_game, "dim_game.parquet", "dim_game")


# dim_date — one row per game date with calendar attributes

print("Building dim_date...")
game_dates = sorted(agg["game_date"].unique())

def get_season_segment(d):
    if d.month in [3, 4]: return "Spring"
    if d.month in [5, 6]: return "Summer"
    if d.month in [7, 8]: return "Late Summer"
    return "Pennant Race"

dim_date = pd.DataFrame([{
    "game_date": d,
    "day_of_week": d.strftime("%A"),
    "day_of_week_num": d.weekday(), # 0 = Monday
    "week_number": d.isocalendar()[1],
    "month": d.month,
    "month_name": d.strftime("%B"),
    "season_segment": get_season_segment(d),
    "days_into_season": (d - SEASON_START).days + 1,
} for d in game_dates])

upload(dim_date, "dim_date.parquet", "dim_date")

print("\nAll dims complete.")
