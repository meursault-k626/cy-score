import boto3
import pandas as pd
import pyarrow.parquet as pq
import numpy as np
import io
import os
from sklearn.preprocessing import RobustScaler
from datetime import date

BUCKET = "baseball-statcast-data-516769757298-ap-southeast-2-an"
SEASON_START = date(2026, 3, 25)
SEASON_END = date(2026, 9, 27)
FULL_SEASON_IP = 162
FORCE = True
TEMP_DIR = r"C:\Users\NizamA\southside_temp"
os.makedirs(TEMP_DIR, exist_ok=True)

s3 = boto3.client("s3")

response = s3.get_object(Bucket=BUCKET, Key="aggregated/pitcher/pitcher_game_agg_2026.parquet")
df_games = pq.read_table(io.BytesIO(response["Body"].read())).to_pandas()
df_games["game_date"] = pd.to_datetime(df_games["game_date"])

# CY Young model is SP-only and regular season only
df_games = df_games[df_games["role"] == "SP"].copy()
df_games = df_games[df_games["game_type"] == "R"].copy()
print(f"Loaded {len(df_games)} SP regular season game rows across {df_games['game_date'].dt.date.nunique()} dates")

def get_min_ip(as_of_date):
    total_days = (SEASON_END - SEASON_START).days
    elapsed_days = (as_of_date - SEASON_START).days
    progress = min(elapsed_days / total_days, 1.0)
    return round(FULL_SEASON_IP * progress * 0.85)

cy_race_weights = {
    "ERA": -2.5,
    "IP": 2.0,
    "FIP": -2.0,
    "K_pct": 1.5,
    "BB_pct": - 1.5,
    "xwOBA_against": -1.5,
    "WHIP": -1.2,
    "swstr_pct": 1.0,
    "delta_run_exp": -1.0,
    "K": 0.8,
    "barrel_pct": -0.8,
    "opponent_wOBA": 0.5
}

race_cols = list(cy_race_weights.keys())
race_weights = np.array([abs(cy_race_weights[c]) for c in race_cols])
race_weights = race_weights / race_weights.sum()

def scale_and_score(df, feature_cols, weights, weight_dict):
    df_scaled = df[feature_cols].copy()
    for col in feature_cols:
        if weight_dict[col] < 0:
            df_scaled[col] = df_scaled[col] * -1
    scaler = RobustScaler()
    scaled = scaler.fit_transform(df_scaled)
    return (scaled * weights).sum(axis=1)

def run_cy_model(df_subset, as_of_date, league):
    season = df_subset.groupby(["pitcher", "player_name", "p_throws"]).agg(
        GS=("game_pk", "count"),
        TBF=("TBF", "sum"),
        BB=("BB", "sum"),
        HR=("HR", "sum"),
        H=("H", "sum"),
        official_ER=("official_ER", "sum"),
        official_W=("official_W", "sum"),
        official_L=("official_L", "sum"),
        K=("K", "sum"),
        IP=("IP", "sum"),
        GB=("GB", "sum"),
        FB=("FB", "sum"),
        LD=("LD", "sum"),
        xwOBA_against=("xwOBA_against", "mean"),
        hard_hit_pct=("hard_hit_pct", "mean"),
        barrel_pct=("barrel_pct", "mean"),
        swstr_pct=("swstr_pct", "mean"),
        o_swing_pct=("o_swing_pct", "mean"),
        z_contact_pct=("z_contact_pct", "mean"),
        first_pitch_strike_pct=("first_pitch_strike_pct", "mean"),
        delta_run_exp=("delta_run_exp", "sum"),
        babip=("babip", "mean"),
        LOB_pct=("LOB_pct", "mean"),
    ).reset_index()

    sos = (
        df_subset.assign(wOBA_TBF=df_subset["opponent_wOBA"] * df_subset["TBF"])
        .groupby("pitcher")[["wOBA_TBF", "TBF"]]
        .sum()
        .assign(opponent_wOBA=lambda x: x["wOBA_TBF"] / x["TBF"])
        ["opponent_wOBA"]
        .reset_index()
    )
    season = season.merge(sos, on="pitcher", how="left")

    season["K_pct"] = season["K"] / season["TBF"].replace(0, 1)
    season["BB_pct"] = season["BB"] / season["TBF"].replace(0, 1)
    season["K_BB_ratio"] = season["K"] / (season["BB"] + 0.001)
    season["HR_per_9"] = season["HR"] / season["IP"].replace(0, 0.001) * 9
    season["WHIP"] = (season["BB"] + season["H"]) / season["IP"].replace(0, 0.001)
    season["ERA"] = (season["official_ER"] / season["IP"].replace(0, 0.001)) * 9
    season["FIP"] = ((13 * season["HR"]) + (3 * season["BB"]) - (2 * season["K"])) / season["IP"].replace(0, 0.001) + 3.10
    season["FIP_ERA_gap"] = season["FIP"] - season["ERA"]
    season["GB_pct"] = season["GB"] / (season["GB"] + season["FB"] + season["LD"]).replace(0, 1)

    min_ip = get_min_ip(as_of_date)
    season = season[season["IP"] >= max(min_ip, 5)].copy()
    if len(season) < 3:
        return None

    progress = min(((as_of_date - SEASON_START).days / (SEASON_END - SEASON_START).days), 1.0)
    ip_elite_threshold = FULL_SEASON_IP * progress * 0.75
    season["workload_bonus"] = 0.0
    season.loc[season["IP"] >= ip_elite_threshold, "workload_bonus"] = 0.03
    season.loc[season["GS"] >= 30, "workload_bonus"] += 0.02

    consistency = df_subset.groupby("pitcher")["FIP"].std().reset_index().rename(columns={"FIP": "FIP_consistency"})
    season = season.merge(consistency, on="pitcher", how="left")
    season["FIP_consistency"] = season["FIP_consistency"].fillna(0)

    season["cy_race_score"] = scale_and_score(season, race_cols, race_weights, cy_race_weights) + season["workload_bonus"]
    season["cy_race_rank"] = season["cy_race_score"].rank(ascending=False).astype(int)
    season["as_of_date"] = as_of_date.isoformat()
    season["league"] = league
    season["min_ip_threshold"] = min_ip

    return season[[
        # Context
        "as_of_date", "league",
        # Rankings
        "cy_race_rank",
        # Identity
        "player_name", "p_throws",
        # Workload
        "GS", "IP", "TBF", "K",
        # Traditional results (voter model drivers)
        "official_W", "official_L", "ERA", "WHIP",
        # ERA quality
        "FIP", "FIP_ERA_gap", "FIP_consistency",
        # Batted ball quality
        "xwOBA_against", "babip", "LOB_pct",
        "hard_hit_pct", "barrel_pct", "GB_pct",
        # Plate discipline
        "K_pct", "BB_pct", "K_BB_ratio",
        "swstr_pct", "o_swing_pct", "z_contact_pct",
        "first_pitch_strike_pct",
        # Run value
        "delta_run_exp", "HR_per_9",
        # Schedule strength
        "opponent_wOBA",
        # Model outputs
        "workload_bonus",
        "cy_race_score",
        "min_ip_threshold"
    ]]

all_dates = sorted(df_games["game_date"].dt.date.unique())
latest_frames = []

for game_date in all_dates:
    date_str = game_date.strftime("%Y%m%d")
    s3_key = f"cy_young/calc_date={game_date}/rankings.parquet"

    if not FORCE:
        try:
            s3.head_object(Bucket=BUCKET, Key=s3_key)
            print(f"[SKIP] {game_date}")
            resp = s3.get_object(Bucket=BUCKET, Key=s3_key)
            latest_frames = [pq.read_table(io.BytesIO(resp["Body"].read())).to_pandas()]
            continue
        except:
            pass

    df_to_date = df_games[df_games["game_date"].dt.date <= game_date]
    frames = []
    for league in ["AL", "NL"]:
        result = run_cy_model(df_to_date[df_to_date["league"] == league], game_date, league)
        if result is not None:
            frames.append(result)

    if not frames:
        print(f"[EMPTY] {game_date}")
        continue

    df_rankings = pd.concat(frames, ignore_index=True)
    local_path = os.path.join(TEMP_DIR, f"rankings_{date_str}.parquet")
    df_rankings.to_parquet(local_path, index=False)
    s3.upload_file(local_path, BUCKET, s3_key)
    os.remove(local_path)
    latest_frames = [df_rankings]
    print(f"[OK] {game_date} — AL: {len(df_rankings[df_rankings['league']=='AL'])} | NL: {len(df_rankings[df_rankings['league']=='NL'])} pitchers")

if latest_frames:
    df_latest = latest_frames[0]
    local_latest = os.path.join(TEMP_DIR, "cy_rankings_latest.parquet")
    df_latest.to_parquet(local_latest, index=False)
    s3.upload_file(local_latest, BUCKET, "cy_young/_cy_young_latest/rankings.parquet")
    os.remove(local_latest)
    print(f"\nLatest rankings written — {df_latest['as_of_date'].iloc[0]}")

print("Backfill complete.")