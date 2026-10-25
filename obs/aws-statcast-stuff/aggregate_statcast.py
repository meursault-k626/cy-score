import boto3
import pandas as pd
import pyarrow.parquet as pq
import io
import os

BUCKET = "baseball-statcast-data-516769757298-ap-southeast-2-an"
PREFIX = "raw/starters/"
OUT_FILE = "pitcher_game_agg_2026.parquet"

al_teams = {"NYY","BOS","TOR","BAL","TB","CLE","MIN","CWS","KC","DET","HOU","ATH","SEA","LAA","TEX"}
nl_teams = {"NYM","ATL","PHI","MIA","WSH","CHC","MIL","STL","PIT","CIN","LAD","SF","AZ","COL","SD"}

s3 = boto3.client("s3")
paginator = s3.get_paginator("list_objects_v2")
dfs = []

for page in paginator.paginate(Bucket=BUCKET, Prefix=PREFIX):
    for obj in page.get("Contents", []):
        key = obj["Key"]
        if not key.endswith(".parquet"):
            continue
        print(f"Reading {key}")
        resp = s3.get_object(Bucket=BUCKET, Key=key)
        dfs.append(pq.read_table(io.BytesIO(resp["Body"].read())).to_pandas())

print(f"\nCombining {len(dfs)} files...")
df = pd.concat(dfs, ignore_index=True)
print(f"Before dedup: {df.shape}")
df = df.drop_duplicates(subset=["game_pk", "pitcher", "at_bat_number", "pitch_number"])
print(f"After dedup:  {df.shape}")

df["batting_team"] = df.apply(lambda r: r["away_team"] if r["inning_topbot"] == "Top" else r["home_team"], axis=1)
df["pitcher_team"] = df.apply(lambda r: r["home_team"] if r["inning_topbot"] == "Top" else r["away_team"], axis=1)
df["opponent_team"] = df["batting_team"]

df_ab = df[df["events"].notna()].copy()
team_offense = (
    df_ab.groupby("batting_team")["woba_value"]
    .mean().reset_index()
    .rename(columns={"woba_value": "opponent_wOBA", "batting_team": "opponent_team"})
)

def classify_event(e):
    if e == "strikeout": return "K"
    elif e in ["walk", "hit_by_pitch"]: return "BB"
    elif e == "home_run": return "HR"
    elif e in ["single", "double", "triple"]: return "H"
    return None

def classify_bb(b):
    if b == "ground_ball": return "GB"
    elif b in ["fly_ball", "popup"]: return "FB"
    elif b == "line_drive": return "LD"
    return None

def get_outs(e):
    if pd.isna(e): return 0
    if e in ["grounded_into_dp", "double_play", "strikeout_double_play"]: return 2
    if e == "triple_play": return 3
    if "out" in e or e in ["sac_fly", "sac_bunt", "strikeout"]: return 1
    return 0

def get_league(pitcher_team):
    if pitcher_team in al_teams: return "AL"
    if pitcher_team in nl_teams: return "NL"
    return "NL"

df["event_class"] = df["events"].apply(classify_event)
df["bb_class"] = df["bb_type"].apply(classify_bb)
df["outs_on_play"] = df["events"].apply(get_outs)
df["is_first_pitch"] = df["pitch_number"] == 1
df["is_first_pitch_strike"] = (df["pitch_number"] == 1) & (df["type"] == "S")
df["in_zone"] = df["zone"].between(1, 9)
df["out_zone"] = ~df["in_zone"]
df["swing"] = df["description"].str.contains("swinging|foul", na=False)
df["o_swing"] = df["out_zone"] & df["swing"]
df["is_z_swing"] = df["in_zone"] & df["swing"]
df["z_contact"] = df["is_z_swing"] & ~df["description"].str.contains("swinging_strike", na=False)
df["is_swstr"] = df["description"].str.contains("swinging_strike", na=False)

# official_ER and official_W are pitch-level repeated per row — take first non-null per game/pitcher
official_cols = df.groupby(["game_pk", "pitcher"]).agg(
    official_ER=("official_ER", "max"),
    official_W=("official_W", "max"),
    official_L=("official_L", "max"),
).reset_index()

agg = df.groupby([
    "game_pk", "game_date", "pitcher", "player_name", "p_throws",
    "home_team", "away_team", "pitcher_team", "opponent_team"
]).agg(
    pitches=("pitch_number", "count"),
    TBF=("events", lambda x: x.notna().sum()),
    K=("event_class", lambda x: (x == "K").sum()),
    BB=("event_class", lambda x: (x == "BB").sum()),
    HR=("event_class", lambda x: (x == "HR").sum()),
    H=("event_class", lambda x: (x == "H").sum()),
    SO=("event_class", lambda x: (x == "K").sum()),
    GB=("bb_class", lambda x: (x == "GB").sum()),
    FB=("bb_class", lambda x: (x == "FB").sum()),
    LD=("bb_class", lambda x: (x == "LD").sum()),
    outs=("outs_on_play", "sum"),
    ER_re24=("delta_pitcher_run_exp", lambda x: x[x < 0].abs().sum()),
    xwOBA_against=("estimated_woba_using_speedangle", "mean"),
    wOBA_against=("woba_value", "mean"),
    babip=("babip_value", "mean"),
    hard_hit_count=("launch_speed", lambda x: (x >= 95).sum()),
    bip_count=("bb_class", lambda x: x.notna().sum()),
    barrel_count=("launch_speed_angle", lambda x: (x == 6).sum()),
    delta_run_exp=("delta_pitcher_run_exp", "sum"),
    swstr_count=("is_swstr", "sum"),
    o_swing_count=("o_swing", "sum"),
    out_zone_count=("out_zone", "sum"),
    z_contact_count=("z_contact", "sum"),
    z_swing_count=("is_z_swing", "sum"),
    fps_count=("is_first_pitch_strike", "sum"),
    fp_count=("is_first_pitch", "sum"),
).reset_index()

# Merge official stats
agg = agg.merge(official_cols, on=["game_pk", "pitcher"], how="left")
agg["official_ER"] = agg["official_ER"].fillna(agg["ER_re24"]).astype(float)
agg["official_W"] = agg["official_W"].fillna(0).astype(int)
agg["official_L"] = agg["official_L"].fillna(0).astype(int)

agg = agg.merge(team_offense, on="opponent_team", how="left")
agg["league"] = agg["pitcher_team"].apply(get_league)
agg["IP"] = agg["outs"] / 3
agg["swstr_pct"] = agg["swstr_count"] / agg["pitches"]
agg["o_swing_pct"] = agg["o_swing_count"] / agg["out_zone_count"].replace(0, 1)
agg["z_contact_pct"] = agg["z_contact_count"] / agg["z_swing_count"].replace(0, 1)
agg["first_pitch_strike_pct"] = agg["fps_count"] / agg["fp_count"].replace(0, 1)
agg["hard_hit_pct"] = agg["hard_hit_count"] / agg["bip_count"].replace(0, 1)
agg["barrel_pct"] = agg["barrel_count"] / agg["bip_count"].replace(0, 1)
agg["K_pct"] = agg["K"] / agg["TBF"].replace(0, 1)
agg["BB_pct"] = agg["BB"] / agg["TBF"].replace(0, 1)
agg["K_BB_ratio"] = agg["K"] / (agg["BB"] + 0.001)
agg["HR_per_9"] = agg["HR"] / agg["IP"].replace(0, 0.001) * 9
agg["WHIP"] = (agg["BB"] + agg["H"]) / agg["IP"].replace(0, 0.001)
agg["ERA"] = (agg["official_ER"] / agg["IP"].replace(0, 0.001)) * 9
agg["ERA_re24"] = (agg["ER_re24"] / agg["IP"].replace(0, 0.001)) * 9
agg["FIP"] = ((13 * agg["HR"]) + (3 * agg["BB"]) - (2 * agg["K"])) / agg["IP"].replace(0, 0.001) + 3.10
agg["FIP_ERA_gap"] = agg["FIP"] - agg["ERA"]
agg["GB_pct"] = agg["GB"] / (agg["GB"] + agg["FB"] + agg["LD"]).replace(0, 1)
agg["LOB_pct"] = 1 - (agg["official_ER"] / (agg["H"] + agg["BB"] - (1.4 * agg["HR"])).replace(0, 1))

agg = agg.drop(columns=[
    "swstr_count", "o_swing_count", "out_zone_count",
    "z_contact_count", "z_swing_count", "fps_count",
    "fp_count", "hard_hit_count", "bip_count", "barrel_count", "outs"
])

agg.to_parquet(OUT_FILE, index=False)
print(f"\nGame-level aggregation: {agg.shape}")
print(agg[agg["player_name"].str.contains("Ohtani", na=False)][["player_name", "game_date", "IP", "ERA", "ERA_re24", "official_W", "pitcher_team", "league"]])

s3.upload_file(OUT_FILE, BUCKET, f"aggregated/{OUT_FILE}")
os.remove(OUT_FILE)
print(f"Uploaded to s3://{BUCKET}/aggregated/{OUT_FILE}")