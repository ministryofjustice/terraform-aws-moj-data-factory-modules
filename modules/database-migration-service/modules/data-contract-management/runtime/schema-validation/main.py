import hashlib
import json
import os
import re
import urllib.parse
import uuid
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from logger import get_logger

logger = get_logger('schema-registration')

s3 = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')

AUDIT_TABLE_NAME = os.environ.get(
    "AUDIT_TABLE_NAME",
    "schema_lifecycle_audit" # default value linking to the testing DynamoDB table
)

CURATED_LAYER_BUCKET_NAME = "curated-layer-test-bucket"

audit_table = dynamodb.Table(AUDIT_TABLE_NAME)

REQUIRED_METADATA = (
    "source",
    "database_name",
    "schema_name",
    "table_name",
    "contract_version",
)


def validate_required_metadata(contract):
    if not isinstance(contract, dict):
        raise ValueError("Contract must be a JSON object.")

    invalid_fields = [
        field
        for field in REQUIRED_METADATA
        if not isinstance(contract.get(field), str)
        or not contract[field].strip()
    ]

    if invalid_fields:
        raise ValueError(
            "Required metadata must contain non-empty strings: "
            + ", ".join(invalid_fields)
        )

    for field in REQUIRED_METADATA:
        if contract[field] != contract[field].strip():
            raise ValueError(
                f"Metadata field '{field}' must not contain "
                "leading or trailing whitespace."
            )

    for field in ("source", "database_name", "schema_name", "table_name"):
        if ":" in contract[field]:
            raise ValueError(
                f"Metadata field '{field}' must not contain ':', "
                "because it separates contract identity fields."
            )

    if not re.fullmatch(r"v[1-9][0-9]*", contract["contract_version"]):
        raise ValueError(
            "Invalid contract_version. Expected v1, v2, v3, etc. "
            "Use lowercase v with no leading zeros. "
            "v0 is reserved for future migration."
        )


def normalise_contract(contract):
    return json.dumps(
        contract,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def calculate_contract_fingerprint(contract):
    """Hash the complete contract for duplicate/conflict detection."""
    normalised = normalise_contract(contract)
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def process_record(record, context):
    try:
        # Read and validate the schema
        object_key = urllib.parse.unquote_plus(record['s3']['object']['key'])
        bucket_name = record['s3']['bucket']['name']

        logger.info("Schema validation started.",
            extra={
                'execution_id': context.aws_request_id,
                'object_key': object_key
            }
        )
        logger.info(f"Reading from S3 ...")
        s3_response = s3.get_object(Bucket=bucket_name, Key=object_key)
        file_bytes = s3_response["Body"].read()
        metadata = json.loads(file_bytes.decode("utf-8"))
        
        logger.info(f"Generating Schema fingerprint ... ")
        schema_fingerprint = calculate_contract_fingerprint(metadata)

        logger.info(f"schema_fingerprint generated :::{schema_fingerprint}")

        validate_required_metadata(metadata)

        source_name = metadata["source"]
        schema_name = metadata["schema_name"]
        db_name = metadata["database_name"]
        table_name = metadata["table_name"]
        contract_version = metadata["contract_version"]

        logger.info("Generating contract ID ...")
        contract_id = str(uuid.uuid5(uuid.NAMESPACE_DNS,f"{source_name}:{db_name}:{schema_name}:{table_name}"))
        
        logger.info(f"Contract ID generated :::{contract_id}")

        # Fetch the existing schema and compare the two
        logger.info("Fetching schema from DynamoDB ...")
        keys = {'contract_id': contract_id, 'contract_version': contract_version}
        existing = audit_table.get_item(Key=keys, ConsistentRead=True)
        item = existing.get('Item')

        if not item:
            logger.error("Contract not found in DynamoDB table.",
                extra={
                    'execution_id': context.aws_request_id,
                    'contract_id': contract_id,
                    'contract_version': contract_version
                }
            )
            raise

        fingerprints_match = schema_fingerprint == item['schema_fingerprint']

        validation_outcome = "SUCCESS" if fingerprints_match else "FAIL"
        validation_summary = "No change" if fingerprints_match else "Schema changed"

        # Publish this result to the audit table and logs
        logger.info(f"Validation outcome: {validation_outcome} \n"
                    f"Validation summary: {validation_summary}",
            extra={
                'execution_id': context.aws_request_id,
                'contract_id': contract_id,
                'contract_version': contract_version
            }
        )

        update_response = audit_table.update_item(
            Key=keys,
            UpdateExpression="SET validation_outcome = :v1, validation_summary = :v2",
            ExpressionAttributeValues={
                ':v1': validation_outcome,
                ':v2': validation_summary
            }
        )

        if fingerprints_match:
            try:
                logger.info("Uploading file to curated layer ...")
                file_name = object_key.split("/")[-1]
                s3.put_object(
                    Bucket=CURATED_LAYER_BUCKET_NAME,
                    Key=file_name,
                    Body=file_bytes
                )
                logger.info("File successfully uploaded to curated layer.",
                    extra={
                        'execution_id': context.aws_request_id,
                        'contract_id': contract_id,
                        'contract_version': contract_version,
                        'file_name': file_name
                    }
                )
            except Exception as e:
                logger.error(f"Error whilst uploading to curated layer: {str(e)}")
        else:
        # If fingerprints did not match, invoke the relevant step function
            pass

    except Exception:
        logger.exception("Schema validation failed.")
        raise


def lambda_handler(event, context):
    for record in event["Records"]:
        process_record(record, context)

    return {
        'statusCode': 200,
        'body': json.dumps('Hello from Lambda!')
    }
