import json
import boto3
import pandas as pd
import io
import base64

BUCKET = 'baseball-statcast-data-516769757298-ap-southeast-2-an'

SUMMARY_COLS = [
    'player_name',
    'current_team',
    'league',
    'p_throws',
    'as_of_date',
    'cy_race_rank',
    'GS',
    'IP',
    'K',
    'official_W',
    'official_L',
    'ERA',
    'WHIP',
    'FIP',
    'FIP_ERA_gap',
    'xwOBA_against',
    'hard_hit_pct',
    'barrel_pct',
    'GB_pct',
    'K_pct',
    'BB_pct',
    'swstr_pct',
    'cy_race_score'
]

ROUND_2DP = ['ERA', 'WHIP', 'FIP', 'FIP_ERA_gap', 'IP']
ROUND_3DP = ['xwOBA_against', 'hard_hit_pct', 'barrel_pct', 'GB_pct', 'K_pct', 'BB_pct', 'swstr_pct', 'cy_race_score']

def lambda_handler(event, context):
    s3 = boto3.client('s3')

    # Load latest rankings
    resp = s3.get_object(Bucket=BUCKET, Key='cy_young/_cy_young_latest/rankings.parquet')
    df = pd.read_parquet(io.BytesIO(resp['Body'].read()))
    df = df.sort_values(['league', 'cy_race_rank'], ascending=[True, True])

    # Load dim_pitcher_team — current team only (team_end_date is null)
    resp_dim = s3.get_object(Bucket=BUCKET, Key='dim/dim_pitcher_team/dim_pitcher_team.parquet')
    dim_pt = pd.read_parquet(io.BytesIO(resp_dim['Body'].read()))
    current_team = dim_pt[dim_pt['team_end_date'].isna()][['player_name', 'team']].rename(columns={'team': 'current_team'})

    # Join current team
    df = df.merge(current_team, on='player_name', how='left')

    # Round
    for col in ROUND_2DP:
        if col in df.columns and pd.api.types.is_numeric_dtype(df[col]):
            df[col] = df[col].round(2)
    for col in ROUND_3DP:
        if col in df.columns and pd.api.types.is_numeric_dtype(df[col]):
            df[col] = df[col].round(3)

    # Select columns
    df = df[[c for c in SUMMARY_COLS if c in df.columns]]

    # Response format
    params = event.get('queryStringParameters') or {}
    fmt = params.get('format', 'json').lower()

    if fmt == 'xlsx':
        buffer = io.BytesIO()
        df.to_excel(buffer, index=False, engine='openpyxl')
        buffer.seek(0)
        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                'Content-Disposition': 'attachment; filename="cy_summary.xlsx"',
            },
            'body': base64.b64encode(buffer.read()).decode('utf-8'),
            'isBase64Encoded': True
        }
    else:
        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'application/json; charset=utf-8'},
            'body': df.to_json(orient='records', force_ascii=False)
        }