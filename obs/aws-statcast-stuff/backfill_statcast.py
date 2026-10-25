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
from datetime import datetime, timedelta, date
import pandas as pd
import boto3
import os
import time

BACKFILL_START = "2026-03-25"
BACKFILL_END = "2026-05-23"
BUCKET = "baseball-statcast-data-516769757298-ap-southeast-2-an"
TEMP_DIR = r"C:\Users\NizamA\southside_temp"
FORCE = True
os.makedirs(TEMP_DIR, exist_ok=True)

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

def get_official_pitcher_stats(date_str):
    try:
        sched_resp = session.get(
            "https://statsapi.mlb.com/api/v1/schedule",
            params={"date": date_str, "sportId": 1},
            timeout=30
        )
        sched = sched_resp.json()
    except Exception as e:
        print(f"  [MLB API ERROR] schedule {date_str} — {e}")
        return pd.DataFrame()
    records = []
    for game_date_entry in sched.get("dates", []):
        for game in game_date_entry.get("games", []):
            game_pk = game.get("gamePk")
            try:
                box_resp = session.get(
                    f"https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore",
                    timeout=30
                )
                box = box_resp.json()
            except Exception as e:
                print(f"  [MLB API ERROR] boxscore {game_pk} — {e}")
                continue
            for side in ["home", "away"]:
                pitchers = box.get("teams", {}).get(side, {}).get("pitchers", [])
                players = box.get("teams", {}).get(side, {}).get("players", {})
                for pitcher_id in pitchers:
                    key = f"ID{pitcher_id}"
                    stats = players.get(key, {}).get("stats", {}).get("pitching", {})
                    if not stats:
                        continue
                    records.append({
                        "game_pk": game_pk,
                        "pitcher": pitcher_id,
                        "official_ER": stats.get("earnedRuns", 0),
                        "official_W": 1 if stats.get("wins", 0) > 0 else 0,
                        "official_L": 1 if stats.get("losses", 0) > 0 else 0,
                        "official_SV": 1 if stats.get("saves", 0) > 0 else 0,
                        "official_HLD": 1 if stats.get("holds", 0) > 0 else 0,
                        "official_BS": 1 if stats.get("blownSaves", 0) > 0 else 0,
                    })
    if not records:
        return pd.DataFrame()
    return pd.DataFrame(records)

def assign_roles(df):
    """
    Label each pitcher row as SP or RP.
    The first pitcher to appear per (game_pk, inning_topbot) is the SP.
    All subsequent pitchers in the same game/side are RP.
    """
    df_sorted = df.sort_values(["game_pk", "inning_topbot", "inning", "at_bat_number"])
    starters = (
        df_sorted
        .groupby(["game_pk", "inning_topbot"])["pitcher"]
        .first()
        .reset_index()
        .rename(columns={"pitcher": "starter_id"})
    )
    df_merged = df_sorted.merge(starters, on=["game_pk", "inning_topbot"])
    df_merged["role"] = df_merged.apply(
        lambda r: "SP" if r["pitcher"] == r["starter_id"] else "RP", axis=1
    )
    return df_merged.drop(columns=["starter_id"])

def process_day(date_str):
    date_nodash = date_str.replace("-", "")
    s3_key = f"raw/game_date={date_str}/statcast_{date_nodash}.parquet"
    local_path = os.path.join(TEMP_DIR, f"statcast_{date_nodash}.parquet")
    s3 = boto3.client("s3")

    if not FORCE:
        try:
            s3.head_object(Bucket=BUCKET, Key=s3_key)
            print(f"[SKIP] {date_str} already in S3")
            return
        except:
            pass

    try:
        df = statcast(start_dt=date_str, end_dt=date_str)
        if df is None or df.empty:
            print(f"[EMPTY] {date_str} — likely off day")
            return

        # Assign SP/RP roles before column selection so starter_id is available
        df = assign_roles(df)

        # Select retained columns plus role
        cols_available = [c for c in pitch_cols if c in df.columns] + ["role"]
        df = df[cols_available].copy()

        # Sort
        df["inning_topbot_order"] = df["inning_topbot"].map({"Top": 0, "Bot": 1})
        df = df.sort_values(
            ["game_pk", "inning_topbot_order", "inning", "at_bat_number", "pitch_number"]
        ).drop(columns=["inning_topbot_order"]).reset_index(drop=True)

        # Merge official stats
        official = get_official_pitcher_stats(date_str)
        if not official.empty:
            df = df.merge(official, on=["game_pk", "pitcher"], how="left")
            for col in ["official_ER", "official_W", "official_L",
                        "official_SV", "official_HLD", "official_BS"]:
                df[col] = df[col].fillna(0).astype(int)
            print(f"  [API] Official stats merged for {date_str}")
        else:
            for col in ["official_ER", "official_W", "official_L",
                        "official_SV", "official_HLD", "official_BS"]:
                df[col] = None
            print(f"  [API] No official stats for {date_str}")

        df.to_parquet(local_path, index=False)
        s3.upload_file(local_path, BUCKET, s3_key)
        os.remove(local_path)
        sp_count = (df["role"] == "SP").any()
        rp_count = (df["role"] == "RP").any()
        print(f"[OK] {date_str} — {len(df)} rows (SP: {df[df['role']=='SP']['pitcher'].nunique()} pitchers, RP: {df[df['role']=='RP']['pitcher'].nunique()} pitchers) → s3://{BUCKET}/{s3_key}")

    except Exception as e:
        print(f"[ERROR] {date_str} — {e}")

start = date.fromisoformat(BACKFILL_START)
end = date.fromisoformat(BACKFILL_END)
dates = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]

print(f"Backfill: {BACKFILL_START} to {BACKFILL_END} — {len(dates)} days")

for d in dates:
    process_day(d)
    time.sleep(3)
