# Test lambda that receives events from EventBridge when GuardDuty reports an unsafe or failed S3 object scan. 
# The lambda can then implement quarantine logic for the affected S3 objects.

import json
import logger

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
            # Quarantine logic will go here

    logger.info(f"Lambda execution completed for object: {object_key} in bucket: {bucket_name}")