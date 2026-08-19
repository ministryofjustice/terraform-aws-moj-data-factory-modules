# Lambda that receives events from EventBridge when GuardDuty reports an unsafe or failed S3 object scan.
# Infected/failed objects are copied to the quarantine bucket and removed from the source bucket.

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3_client = boto3.client("s3")

QUARANTINE_BUCKET_NAME = os.environ["QUARANTINE_BUCKET_NAME"]
QUARANTINE_KMS_KEY_ARN = os.environ["QUARANTINE_KMS_KEY_ARN"]


def quarantine_object(bucket_name, object_key):
    """Copy the object to the quarantine bucket then delete it from the source bucket."""
    s3_client.copy_object(
        Bucket=QUARANTINE_BUCKET_NAME,
        Key=object_key,
        CopySource={"Bucket": bucket_name, "Key": object_key},
        ServerSideEncryption="aws:kms",
        SSEKMSKeyId=QUARANTINE_KMS_KEY_ARN,
    )
    s3_client.delete_object(Bucket=bucket_name, Key=object_key)


def lambda_handler(event, context):
    # Log the incoming event
    logger.info("Received event: " + json.dumps(event, indent=2))

    # Extract relevant information from the event
    detail = event.get('detail', {})

    for object in detail.get('s3ObjectDetails', []):
        bucket_name = object.get('bucketName')
        object_key = object.get('objectKey')
        scan_result_status = detail.get('scanResultDetails', {}).get('scanResultStatus')
        scan_result = detail.get('scanResultDetails', {}).get('scanResult')

        # Log the extracted information
        logger.info(f"Bucket: {bucket_name}, Object Key: {object_key}, Scan Result Status: {scan_result_status}, Scan Result: {scan_result}")

        if scan_result in ["THREATS_FOUND", "FAILED", "ACCESS_DENIED"]:
            logger.info(f"Quarantining object {object_key} in bucket {bucket_name} due to scan result: {scan_result} with scan result status: {scan_result_status}")
            quarantine_object(bucket_name, object_key)

    logger.info(f"Lambda execution completed for object: {object_key} in bucket: {bucket_name}")