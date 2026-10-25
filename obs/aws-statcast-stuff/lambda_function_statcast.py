import os
import pathlib

# Must run before any pybaseball import — pybaseball calls pathlib.Path.home()
# at import time to create its cache directory. Lambda's home is read-only;
# /tmp is the only writable path.
os.makedirs("/tmp/.pybaseball/cache", exist_ok=True)
pathlib.Path.home = classmethod(lambda cls: pathlib.Path("/tmp"))

import ssl
import urllib3
import warnings
import requests
from requests.adapters import HTTPAdapter

ssl._create_default_https_context = ssl._create_unverified_context
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

session = requests.Session()
session.verify = False
adapter = HTTPAdapter()
session.mount("https://", adapter)

from pybaseball import statcast
from datetime import date, timedelta
import pandas as pd
import pyarrow.parquet as pq
import numpy as np
import boto3
import io
from sklearn.preprocessing import RobustScaler

BUCKET = "baseball-statcast-data-516769757298-ap-southeast-2-an"
SEASON_START = date(2026, 3, 25)
SEASON_END = date(2026, 9, 27)
FULL_SEASON_IP = 162

al_teams = {"NYY","BOS","TOR","BAL","TB","CLE","MIN","CWS","KC","DET","HOU","ATH","SEA","LAA","TEX"}
nl_teams = {"NYM","ATL","PHI","MIA","WSH","CHC","MIL","STL","PIT","CIN","LAD","SF","AZ","COL","SD"}

pitch_cols = [
    "game_pk", "game_date", "game_year", "game_type",
    "pitcher", "player_name", "p_throws",
    "home_team", "away_team",
    "inning", "inning_topbot", "at_bat_number", "pitch_number",
    "n_thruorder_pitcher", "n_priorpa_thisgame_player_at_bat",
    "pitcher_days_since_prev_game", "pitcher_days_until_next_game",
    "age_pit",
    "balls", "strikes", "outs_when_up",
    "on_1b", "on_2b", "on_3b", "stand",
    "pitch_type", "pitch_name",
    "release_speed", "effective_speed",
    "release_spin_rate", "spin_axis",
    "release_extension", "release_pos_x", "release_pos_z", "release_pos_y",
    "pfx_x", "pfx_z",
    "api_break_z_with_gravity", "api_break_x_arm", "api_break_x_batter_in",
    "arm_angle",
    "plate_x", "plate_z", "sz_top", "sz_bot", "zone",
    "type", "description", "events", "bb_type",
    "launch_speed", "launch_angle", "hit_distance_sc", "launch_speed_angle",
    "estimated_ba_using_speedangle", "estimated_woba_using_speedangle",
    "woba_value", "woba_denom", "babip_value", "iso_value",
    "delta_run_exp", "delta_pitcher_run_exp",
    "home_score", "away_score", "bat_score", "fld_score",
    "post_home_score", "post_away_score", "post_bat_score", "post_fld_score",
]


# CY Young model config

cy_race_weights = {
    "ERA": -2.5,
    "IP": 2.0,
    "FIP": -2.0,
    "K_pct": 1.5,
    "BB_pct": -1.5,
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


# HELPERS


def get_official_pitcher_stats(date_str):
    try:
        sched = session.get(
            "https://statsapi.mlb.com/api/v1/schedule",
            params={"date": date_str, "sportId": 1}, timeout=30
        ).json()
    except Exception as e:
        print(f"  [MLB API ERROR] schedule {date_str} — {e}")
        return pd.DataFrame()

    records = []
    for entry in sched.get("dates", []):
        for game in entry.get("games", []):
            game_pk = game.get("gamePk")
            try:
                box = session.get(
                    f"https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore",
                    timeout=30
                ).json()
            except Exception as e:
                print(f"  [MLB API ERROR] boxscore {game_pk} — {e}")
                continue
            for side in ["home", "away"]:
                pitchers = box.get("teams", {}).get(side, {}).get("pitchers", [])
                players  = box.get("teams", {}).get(side, {}).get("players", {})
                for pid in pitchers:
                    stats = players.get(f"ID{pid}", {}).get("stats", {}).get("pitching", {})
                    if not stats:
                        continue
                    records.append({
                        "game_pk": game_pk,
                        "pitcher": pid,
                        "official_ER": stats.get("earnedRuns", 0),
                        "official_W": 1 if stats.get("wins", 0) > 0 else 0,
                        "official_L": 1 if stats.get("losses", 0) > 0 else 0,
                        "official_SV": 1 if stats.get("saves", 0) > 0 else 0,
                        "official_HLD": 1 if stats.get("holds", 0) > 0 else 0,
                        "official_BS": 1 if stats.get("blownSaves", 0) > 0 else 0,
                    })
    return pd.DataFrame(records) if records else pd.DataFrame()


def assign_roles(df):
    df_sorted = df.sort_values(["game_pk", "inning_topbot", "inning", "at_bat_number"])
    starters = (
        df_sorted.groupby(["game_pk", "inning_topbot"])["pitcher"]
        .first().reset_index()
        .rename(columns={"pitcher": "starter_id"})
    )
    df_merged = df_sorted.merge(starters, on=["game_pk", "inning_topbot"])
    df_merged["role"] = df_merged.apply(
        lambda r: "SP" if r["pitcher"] == r["starter_id"] else "RP", axis=1
    )
    return df_merged.drop(columns=["starter_id"])


def classify_event(e):
    if e == "strikeout": return "K"
    if e in ["walk", "hit_by_pitch"]: return "BB"
    if e == "home_run": return "HR"
    if e in ["single", "double", "triple"]: return "H"
    return None

def classify_bb(b):
    if b == "ground_ball": return "GB"
    if b in ["fly_ball", "popup"]: return "FB"
    if b == "line_drive": return "LD"
    return None

def get_outs(e):
    if pd.isna(e): return 0
    if e in ["grounded_into_dp", "double_play", "strikeout_double_play"]: return 2
    if e == "triple_play": return 3
    if "out" in e or e in ["sac_fly", "sac_bunt", "strikeout"]: return 1
    return 0

def get_league(t):
    if t in al_teams: return "AL"
    if t in nl_teams: return "NL"
    return "NL"


def build_aggregation(df):
    """Roll pitch-level data up to one row per pitcher per game."""
    df["batting_team"] = df.apply(lambda r: r["away_team"] if r["inning_topbot"] == "Top" else r["home_team"], axis=1)
    df["pitcher_team"] = df.apply(lambda r: r["home_team"] if r["inning_topbot"] == "Top" else r["away_team"], axis=1)
    df["opponent_team"] = df["batting_team"]

    # Strength of schedule
    team_offense = (
        df[df["events"].notna()].groupby("batting_team")["woba_value"]
        .mean().reset_index()
        .rename(columns={"woba_value": "opponent_wOBA", "batting_team": "opponent_team"})
    )

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

    # Inherited runners
    df_sorted = df.sort_values(["game_pk", "pitcher", "inning", "at_bat_number", "pitch_number"])
    entry = df_sorted.groupby(["game_pk", "pitcher"]).first().reset_index()
    entry["inherited_runners"] = entry[["on_1b", "on_2b", "on_3b"]].notna().sum(axis=1)
    inning_end = (
        df_sorted.groupby(["game_pk", "inning", "inning_topbot"])["post_bat_score"]
        .max().reset_index().rename(columns={"post_bat_score": "inning_end_bat_score"})
    )
    entry = entry.merge(inning_end, on=["game_pk", "inning", "inning_topbot"], how="left")
    entry["runs_after_entry"] = (entry["inning_end_bat_score"] - entry["bat_score"]).clip(lower=0)
    entry["inherited_runners_scored"] = entry.apply(
        lambda r: int(min(r["inherited_runners"], r["runs_after_entry"])) if r["inherited_runners"] > 0 else 0,
        axis=1
    )
    rp_ir = entry[["game_pk", "pitcher", "inherited_runners", "inherited_runners_scored"]]

    # Official stats
    official = df.groupby(["game_pk", "pitcher"]).agg(
        official_ER=("official_ER", "max"),
        official_W=("official_W", "max"),
        official_L=("official_L", "max"),
        official_SV=("official_SV", "max"),
        official_HLD=("official_HLD", "max"),
        official_BS=("official_BS", "max"),
    ).reset_index()

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

    agg = agg.merge(official, on=["game_pk", "pitcher"], how="left")
    agg = agg.merge(team_offense, on="opponent_team", how="left")
    agg = agg.merge(rp_ir, on=["game_pk", "pitcher"], how="left")

    agg["official_ER"] = agg["official_ER"].fillna(agg["ER_re24"]).astype(float)
    for col in ["official_W", "official_L", "official_SV", "official_HLD", "official_BS"]:
        agg[col] = agg[col].fillna(0).astype(int)

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

    sp_mask = agg["role"] == "SP"
    rp_mask = agg["role"] == "RP"

    agg["LOB_pct"] = None
    denom = (agg.loc[sp_mask, "H"] + agg.loc[sp_mask, "BB"] - (1.4 * agg.loc[sp_mask, "HR"])).replace(0, 1)
    agg.loc[sp_mask, "LOB_pct"] = 1 - (agg.loc[sp_mask, "official_ER"] / denom)

    agg["IR_stranding_pct"] = None
    ir_mask = rp_mask & (agg["inherited_runners"] > 0)
    agg.loc[ir_mask, "IR_stranding_pct"] = (
        1 - (agg.loc[ir_mask, "inherited_runners_scored"] / agg.loc[ir_mask, "inherited_runners"])
    )
    agg.loc[sp_mask, "inherited_runners"]        = None
    agg.loc[sp_mask, "inherited_runners_scored"] = None

    return agg.drop(columns=[
        "swstr_count", "o_swing_count", "out_zone_count", "z_contact_count",
        "z_swing_count", "fps_count", "fp_count", "hard_hit_count", "bip_count",
        "barrel_count", "outs"
    ])


def scale_and_score(df, feature_cols, weights, weight_dict):
    df_scaled = df[feature_cols].copy()
    for col in feature_cols:
        if weight_dict[col] < 0:
            df_scaled[col] *= -1
    scaled = RobustScaler().fit_transform(df_scaled)
    return (scaled * weights).sum(axis=1)


def get_min_ip(as_of_date):
    progress = min((as_of_date - SEASON_START).days / (SEASON_END - SEASON_START).days, 1.0)
    return round(FULL_SEASON_IP * progress * 0.85)


def run_cy_model(df_subset, as_of_date, league):
    season = df_subset.groupby(["pitcher", "player_name", "p_throws"]).agg(
        GS=("game_pk",              "count"),
        TBF=("TBF",                 "sum"),
        K=("K",                     "sum"),
        BB=("BB",                   "sum"),
        HR=("HR",                   "sum"),
        H=("H",                     "sum"),
        official_ER=("official_ER", "sum"),
        official_W=("official_W",   "sum"),
        official_L=("official_L",   "sum"),
        IP=("IP",                   "sum"),
        GB=("GB",                   "sum"),
        FB=("FB",                   "sum"),
        LD=("LD",                   "sum"),
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
        .groupby("pitcher")[["wOBA_TBF", "TBF"]].sum()
        .assign(opponent_wOBA=lambda x: x["wOBA_TBF"] / x["TBF"])["opponent_wOBA"]
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

    progress = min((as_of_date - SEASON_START).days / (SEASON_END - SEASON_START).days, 1.0)
    ip_elite = FULL_SEASON_IP * progress * 0.75
    season["workload_bonus"] = 0.0
    season.loc[season["IP"] >= ip_elite, "workload_bonus"] = 0.03
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
        "as_of_date", "league",
        "cy_race_rank",
        "player_name", "p_throws",
        "GS", "IP", "TBF", "K",
        "official_W", "official_L", "ERA", "WHIP",
        "FIP", "FIP_ERA_gap", "FIP_consistency",
        "xwOBA_against", "babip", "LOB_pct",
        "hard_hit_pct", "barrel_pct", "GB_pct",
        "K_pct", "BB_pct", "K_BB_ratio",
        "swstr_pct", "o_swing_pct", "z_contact_pct", "first_pitch_strike_pct",
        "delta_run_exp", "HR_per_9",
        "opponent_wOBA",
        "workload_bonus", "cy_race_score",
        "min_ip_threshold",
    ]]


# LAMBDA HANDLER


def lambda_handler(event, context):
    s3 = boto3.client("s3")
    target_date = (date.today() - timedelta(days=1)).isoformat()
    date_nodash = target_date.replace("-", "")
    print(f"Target date: {target_date}")


    # Step 1 — Pull raw Statcast data

    print("Pulling Statcast data...")
    df = statcast(start_dt=target_date, end_dt=target_date)
    if df is None or df.empty:
        print(f"[EMPTY] {target_date} — likely off day")
        return {"statusCode": 200, "body": f"No data for {target_date}"}

    df = assign_roles(df)
    cols_available = [c for c in pitch_cols if c in df.columns] + ["role"]
    df = df[cols_available].copy()

    df["inning_topbot_order"] = df["inning_topbot"].map({"Top": 0, "Bot": 1})
    df = df.sort_values(
        ["game_pk", "inning_topbot_order", "inning", "at_bat_number", "pitch_number"]
    ).drop(columns=["inning_topbot_order"]).reset_index(drop=True)

    official = get_official_pitcher_stats(target_date)
    if not official.empty:
        df = df.merge(official, on=["game_pk", "pitcher"], how="left")
        for col in ["official_ER", "official_W", "official_L", "official_SV", "official_HLD", "official_BS"]:
            df[col] = df[col].fillna(0).astype(int)
        print(f"  [API] Official stats merged")
    else:
        for col in ["official_ER", "official_W", "official_L", "official_SV", "official_HLD", "official_BS"]:
            df[col] = None
        print(f"  [API] No official stats available")

    raw_key   = f"raw/game_date={target_date}/statcast_{date_nodash}.parquet"
    raw_local = f"/tmp/statcast_{date_nodash}.parquet"
    df.to_parquet(raw_local, index=False)
    s3.upload_file(raw_local, BUCKET, raw_key)
    os.remove(raw_local)
    print(f"Raw saved → {raw_key} ({len(df)} rows, SP: {(df['role']=='SP').sum()}, RP: {(df['role']=='RP').sum()})")


    # Step 2 — Load all raw files, deduplicate, aggregate

    print("Loading all raw files...")
    paginator = s3.get_paginator("list_objects_v2")
    dfs = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix="raw/"):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith(".parquet"):
                continue
            resp = s3.get_object(Bucket=BUCKET, Key=obj["Key"])
            dfs.append(pq.read_table(io.BytesIO(resp["Body"].read())).to_pandas())

    df_all = pd.concat(dfs, ignore_index=True)
    df_all = df_all.drop_duplicates(subset=["game_pk", "pitcher", "at_bat_number", "pitch_number"])
    print(f"Combined: {df_all.shape} after dedup")

    agg = build_aggregation(df_all)

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

    agg["game_date"] = pd.to_datetime(agg["game_date"]).dt.date

    agg_local = "/tmp/pitcher_game_agg_2026.parquet"
    agg.to_parquet(agg_local, index=False)
    s3.upload_file(agg_local, BUCKET, "aggregated/pitcher/pitcher_game_agg_2026.parquet")
    os.remove(agg_local)
    print(f"Aggregated saved — {len(agg)} rows (SP: {(agg['role']=='SP').sum()}, RP: {(agg['role']=='RP').sum()})")


    # Step 2b — Update dim tables

    print("Updating dim tables...")
    # Load full aggregated file for dim rebuilds
    agg_full = pd.read_parquet(io.BytesIO(
        s3.get_object(Bucket=BUCKET, Key="aggregated/pitcher/pitcher_game_agg_2026.parquet")["Body"].read()
    ))
    agg_full["game_date"] = pd.to_datetime(agg_full["game_date"]).dt.date

    # dim_pitcher
    dim_pitcher = (
        agg_full.sort_values("game_date")
        .groupby("pitcher")
        .agg(player_name=("player_name", "last"), p_throws=("p_throws", "last"))
        .reset_index()
    )
    dim_pitcher_local = "/tmp/dim_pitcher.parquet"
    dim_pitcher.to_parquet(dim_pitcher_local, index=False)
    s3.upload_file(dim_pitcher_local, BUCKET, "dim/dim_pitcher/dim_pitcher.parquet")
    os.remove(dim_pitcher_local)
    print(f"dim_pitcher updated — {len(dim_pitcher)} pitchers")

    # dim_pitcher_team — SCD Type 2
    player_games = (
        agg_full[["pitcher", "player_name", "game_date", "pitcher_team"]]
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
                    stints.append({
                        "pitcher": pitcher_id,
                        "player_name": player_name,
                        "team": current_team,
                        "team_start_date": stint_start,
                        "team_end_date": row["game_date"] - timedelta(days=1),
                    })
                current_team = row["pitcher_team"]
                stint_start = row["game_date"]
        stints.append({
            "pitcher": pitcher_id,
            "player_name": player_name,
            "team": current_team,
            "team_start_date": stint_start,
            "team_end_date": None,
        })
    dim_pitcher_team = pd.DataFrame(stints)
    dim_pt_local = "/tmp/dim_pitcher_team.parquet"
    dim_pitcher_team.to_parquet(dim_pt_local, index=False)
    s3.upload_file(dim_pt_local, BUCKET, "dim/dim_pitcher_team/dim_pitcher_team.parquet")
    os.remove(dim_pt_local)
    print(f"dim_pitcher_team updated — {len(dim_pitcher_team)} stints")

    # dim_game — append today's games
    try:
        existing_game = pd.read_parquet(io.BytesIO(
            s3.get_object(Bucket=BUCKET, Key="dim/dim_game/dim_game.parquet")["Body"].read()
        ))
    except:
        existing_game = pd.DataFrame(columns=["game_pk","game_date","game_type","home_team","away_team"])
    new_games = (
        agg[agg["game_date"] == pd.to_datetime(target_date).date()]
        [["game_pk","game_date","game_type","home_team","away_team"]]
        .drop_duplicates(subset=["game_pk"])
    )
    dim_game = pd.concat([existing_game, new_games], ignore_index=True).drop_duplicates(subset=["game_pk"])
    dim_game_local = "/tmp/dim_game.parquet"
    dim_game.to_parquet(dim_game_local, index=False)
    s3.upload_file(dim_game_local, BUCKET, "dim/dim_game/dim_game.parquet")
    os.remove(dim_game_local)
    print(f"dim_game updated — {len(dim_game)} games")

    # dim_date — append if not present
    try:
        existing_date = pd.read_parquet(io.BytesIO(
            s3.get_object(Bucket=BUCKET, Key="dim/dim_date/dim_date.parquet")["Body"].read()
        ))
    except:
        existing_date = pd.DataFrame(columns=["game_date"])
    target_date_obj2 = date.fromisoformat(target_date)
    def get_season_segment(d):
        if d.month in [3, 4]: return "Spring"
        if d.month in [5, 6]: return "Summer"
        if d.month in [7, 8]: return "Late Summer"
        return "Pennant Race"
    if target_date_obj2 not in existing_date["game_date"].values:
        new_date_row = pd.DataFrame([{
            "game_date": target_date_obj2,
            "day_of_week": target_date_obj2.strftime("%A"),
            "day_of_week_num": target_date_obj2.weekday(),
            "week_number": target_date_obj2.isocalendar()[1],
            "month": target_date_obj2.month,
            "month_name": target_date_obj2.strftime("%B"),
            "season_segment": get_season_segment(target_date_obj2),
            "days_into_season": (target_date_obj2 - SEASON_START).days + 1,
        }])
        dim_date = pd.concat([existing_date, new_date_row], ignore_index=True)
        dim_date_local = "/tmp/dim_date.parquet"
        dim_date.to_parquet(dim_date_local, index=False)
        s3.upload_file(dim_date_local, BUCKET, "dim/dim_date/dim_date.parquet")
        os.remove(dim_date_local)
        print(f"dim_date updated — added {target_date}")
    else:
        print(f"dim_date — {target_date} already present, skipped")


    # Step 3 — CY Young model (SP only)
    print("Running CY Young model...")
    agg["game_date"] = pd.to_datetime(agg["game_date"])
    target_date_obj = date.fromisoformat(target_date)
    df_sp = agg[(agg["role"] == "SP") & (agg["game_type"] == "R")].copy()

    frames = []
    for league in ["AL", "NL"]:
        result = run_cy_model(
            df_sp[df_sp["league"] == league],
            target_date_obj,
            league
        )
        if result is not None:
            frames.append(result)

    if not frames:
        print("[CY YOUNG] Not enough qualified pitchers yet")
        return {"statusCode": 200, "body": f"Pipeline complete for {target_date} — no CY Young output"}

    df_rankings = pd.concat(frames, ignore_index=True)

    # Daily snapshot
    cy_key   = f"cy_young/calc_date={target_date}/rankings.parquet"
    cy_local = f"/tmp/rankings_{date_nodash}.parquet"
    df_rankings.to_parquet(cy_local, index=False)
    s3.upload_file(cy_local, BUCKET, cy_key)
    os.remove(cy_local)

    # Latest file (read by API Lambda)
    latest_local = "/tmp/cy_rankings_latest.parquet"
    df_rankings.to_parquet(latest_local, index=False)
    s3.upload_file(latest_local, BUCKET, "cy_young/_cy_young_latest/rankings.parquet")
    os.remove(latest_local)

    print(f"CY Young saved → {cy_key}")
    print(f"AL: {len(df_rankings[df_rankings['league']=='AL'])} | NL: {len(df_rankings[df_rankings['league']=='NL'])} pitchers ranked")
    return {"statusCode": 200, "body": f"Pipeline complete for {target_date}"}