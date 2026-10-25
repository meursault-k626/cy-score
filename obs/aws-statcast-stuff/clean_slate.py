import boto3

BUCKET = "baseball-statcast-data-516769757298-ap-southeast-2-an"

s3 = boto3.client("s3")

print(f"Deleting all objects in s3://{BUCKET} ...")

paginator = s3.get_paginator("list_objects_v2")
pages = paginator.paginate(Bucket=BUCKET)

deleted_count = 0

for page in pages:
    objects = page.get("Contents", [])
    if not objects:
        continue
    delete_payload = {"Objects": [{"Key": obj["Key"]} for obj in objects]}
    s3.delete_objects(Bucket=BUCKET, Delete=delete_payload)
    deleted_count += len(objects)
    for obj in objects:
        print(f"  [DELETED] {obj['Key']}")

print(f"\nDone — {deleted_count} objects deleted from s3://{BUCKET}")
