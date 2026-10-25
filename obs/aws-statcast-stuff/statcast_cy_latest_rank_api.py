import json
import boto3
import pandas as pd
import io
import base64

def lambda_handler(event, context):
    s3 = boto3.client('s3')
    bucket = 'baseball-statcast-data-516769757298-ap-southeast-2-an'
    key = 'cy_young/_cy_young_latest/rankings.parquet'

    response = s3.get_object(Bucket=bucket, Key=key)
    df = pd.read_parquet(io.BytesIO(response['Body'].read()))
    df = df.sort_values(['cy_race_rank'], ascending=[True])

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
                'Content-Disposition': 'attachment; filename="cy_rankings.xlsx"',
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