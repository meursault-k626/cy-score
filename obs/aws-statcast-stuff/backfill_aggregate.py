import boto3
import pandas as pd
import pyarrow.parquet as pq
import io
import os

BUCKET = "baseball-statcast-data-516769757298-ap-southeast-2-an"
RAW_PREFIX = "raw/"
OUT_FILE = "pitcher_game_agg_2026.parquet"
TEMP_DIR = r"C:\Users\NizamA\southside_temp"
os.makedirs(TEMP_DIR, exist_ok=True)

al_teams = {"NYY","BOS","TOR","BAL","TB","CLE","MIN","CWS","KC","DET","HOU","ATH","SEA","LAA","TEX"}
nl_teams = {"NYM","ATL","PHI","MIA","WSH","CHC","MIL","STL","PIT","CIN","LAD","SF","AZ","COL","SD"}


# Load all raw parquet files

s3 = boto3.client("s3")
paginator = s3.get_paginator("list_objects_v2")
dfs = []

for page in paginator.paginate(Bucket=BUCKET, Prefix=RAW_PREFIX):
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


# Derive team context

df["batting_team"] = df.apply(
    lambda r: r["away_team"] if r["inning_topbot"] == "Top" else r["home_team"], axis=1
)
df["pitcher_team"] = df.apply(
    lambda r: r["home_team"] if r["inning_topbot"] == "Top" else r["away_team"], axis=1
)
df["opponent_team"] = df["batting_team"]


# Strength of schedule — season-wide team offensive wOBA

df_ab = df[df["events"].notna()].copy()
team_offense = (
    df_ab.groupby("batting_team")["woba_value"]
    .mean().reset_index()
    .rename(columns={"woba_value": "opponent_wOBA", "batting_team": "opponent_team"})
)


# Feature engineering

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


# Inherited runners (RP-specific)
# For each pitcher's first appearance in a game, count runners on base at
# entry.
# Inherited runners scored = runs that scored in the entry inning after
# the RP took over, capped at inherited_runners count.

df_sorted = df.sort_values(
    ["game_pk", "pitcher", "inning", "at_bat_number", "pitch_number"]
)

entry_pitches = df_sorted.groupby(["game_pk", "pitcher"]).first().reset_index()

entry_pitches["inherited_runners"] = (
    entry_pitches[["on_1b", "on_2b", "on_3b"]].notna().sum(axis=1)
)

# End-of-inning bat score for the entry inning
inning_end_scores = (
    df_sorted
    .groupby(["game_pk", "inning", "inning_topbot"])["post_bat_score"]
    .max()
    .reset_index()
    .rename(columns={"post_bat_score": "inning_end_bat_score"})
)

entry_pitches = entry_pitches.merge(
    inning_end_scores, on=["game_pk", "inning", "inning_topbot"], how="left"
)

entry_pitches["runs_after_entry"] = (
    entry_pitches["inning_end_bat_score"] - entry_pitches["bat_score"]
).clip(lower=0)

entry_pitches["inherited_runners_scored"] = entry_pitches.apply(
    lambda r: int(min(r["inherited_runners"], r["runs_after_entry"]))
    if r["inherited_runners"] > 0 else 0,
    axis=1
)

rp_ir = entry_pitches[
    ["game_pk", "pitcher", "inherited_runners", "inherited_runners_scored"]
].copy()


# Official stats — take max per game/pitcher (values are repeated pitch-level)

official_cols = df.groupby(["game_pk", "pitcher"]).agg(
    official_ER=("official_ER", "max"),
    official_W=("official_W", "max"),
    official_L=("official_L", "max"),
    official_SV=("official_SV", "max"),
    official_HLD=("official_HLD", "max"),
    official_BS=("official_BS", "max"),
).reset_index()


# Main aggregation — one row per pitcher per game

agg = df.groupby([
    "game_pk", "game_date", "game_type", "pitcher", "player_name", "p_throws",
    "home_team", "away_team", "pitcher_team", "opponent_team", "role"
]).agg(
    pitches=("pitch_number", "count"),
    TBF=("events", lambda x: x.notna().sum()),
    K=("event_class", lambda x: (x == "K").sum()),
    BB=("event_class", lambda x: (x == "BB").sum()),
    HR=("event_class", lambda x: (x == "HR").sum()),
    H=("event_class", lambda x: (x == "H").sum()),
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


# Merge lookups

agg = agg.merge(official_cols, on=["game_pk", "pitcher"], how="left")
agg["official_ER"] = agg["official_ER"].fillna(agg["ER_re24"]).astype(float)
for col in ["official_W", "official_L", "official_SV", "official_HLD", "official_BS"]:
    agg[col] = agg[col].fillna(0).astype(int)

agg = agg.merge(team_offense, on="opponent_team", how="left")
agg = agg.merge(rp_ir, on=["game_pk", "pitcher"], how="left")


# Derived columns

agg["league"] = agg["pitcher_team"].apply(get_league)
agg["GS"] = (agg["role"] == "SP").astype(int)
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
agg["wOBA_TBF_product"] = agg["opponent_wOBA"] * agg["TBF"]

# SP-only: LOB_pct — null for RP
sp_mask = agg["role"] == "SP"
rp_mask = agg["role"] == "RP"
agg["LOB_pct"] = None
denom = (
    agg.loc[sp_mask, "H"] + agg.loc[sp_mask, "BB"] - (1.4 * agg.loc[sp_mask, "HR"])
).replace(0, 1)
agg.loc[sp_mask, "LOB_pct"] = 1 - (agg.loc[sp_mask, "official_ER"] / denom)

# RP-only: inherited runner stranding — null for SP and RP with no inherited runners
agg["IR_stranding_pct"] = None
ir_mask = rp_mask & (agg["inherited_runners"] > 0)
agg.loc[ir_mask, "IR_stranding_pct"] = (
    1 - (agg.loc[ir_mask, "inherited_runners_scored"] / agg.loc[ir_mask, "inherited_runners"])
)

# SP: null out inherited runner columns
agg.loc[sp_mask, "inherited_runners"] = None
agg.loc[sp_mask, "inherited_runners_scored"] = None


# Drop intermediate columns

agg = agg.drop(columns=[
    "swstr_count", "o_swing_count", "out_zone_count",
    "z_contact_count", "z_swing_count", "fps_count",
    "fp_count", "hard_hit_count", "bip_count", "barrel_count", "outs"
])



# Rounding — only columns where fixed precision makes sense
# Left as-is: IP (fractional thirds), K_BB_ratio, wOBA_TBF_product (intermediate)

rate_3dp = [
    "ERA", "ERA_re24", "FIP", "WHIP", "FIP_ERA_gap",
    "xwOBA_against", "wOBA_against", "babip",
    "K_pct", "BB_pct", "swstr_pct", "o_swing_pct",
    "z_contact_pct", "first_pitch_strike_pct", "hard_hit_pct", "barrel_pct",
    "GB_pct", "HR_per_9", "opponent_wOBA",
]
score_4dp = ["delta_run_exp"]

for col in rate_3dp:
    if col in agg.columns and pd.api.types.is_numeric_dtype(agg[col]):
        agg[col] = agg[col].round(3)
for col in score_4dp:
    if col in agg.columns and pd.api.types.is_numeric_dtype(agg[col]):
        agg[col] = agg[col].round(4)
# LOB_pct and IR_stranding_pct are object dtype due to conditional assignment — cast and round
for col in ["LOB_pct", "IR_stranding_pct"]:
    if col in agg.columns:
        agg[col] = pd.to_numeric(agg[col], errors="coerce").round(3)


# Upload

agg["game_date"] = pd.to_datetime(agg["game_date"]).dt.date

local_path = os.path.join(TEMP_DIR, OUT_FILE)
agg.to_parquet(local_path, index=False)

print(f"\nAggregation complete: {agg.shape}")
print(f"SP rows: {(agg['role'] == 'SP').sum()} | RP rows: {(agg['role'] == 'RP').sum()}")
print(agg[agg["player_name"].str.contains("Ohtani", na=False)][
    ["player_name", "role", "game_date", "IP", "ERA", "FIP", "official_W", "pitcher_team", "league"]
])

s3.upload_file(local_path, BUCKET, f"aggregated/pitcher/{OUT_FILE}")
os.remove(local_path)
print(f"\nUploaded to s3://{BUCKET}/aggregated/{OUT_FILE}")
