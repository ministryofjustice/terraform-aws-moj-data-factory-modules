import json
import hashlib
import os
import boto3
import urllib.parse
import uuid
import re

from logger import get_logger
from datetime import datetime, timezone
from fastavro.schema import to_parsing_canonical_form
from botocore.exceptions import ClientError


logger = get_logger('schema-registration')

s3 = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')

AUDIT_TABLE_NAME = os.environ.get(
    "AUDIT_TABLE_NAME",
    "schema_lifecycle_audit" # default value linking to the testing DynamoDB table
)

audit_table = dynamodb.Table(AUDIT_TABLE_NAME)



def increment_contract_version(contract_version: str) -> str:
    match = re.match(r"v(\d+)$", contract_version.lower())

    if not match:
        raise ValueError(
             f"Invalid contract_version '{contract_version}'. Expected format: v<number>"
        )

    version_num = int(match.group(1))
    return f"v{version_num + 1}"


def normalise_contract(contract: dict) -> str:
    """
    Produce a deterministic JSON representation of a contract.

    - Sort object keys recursively.
    - Remove insignificant whitespace.
    - Preserve array ordering.
    - Preserve all contract attributes including:
    * defaults
    * logicalType
    * precision
    * scale
    * aliases
    * custom metadata
    """

    return json.dumps(
    contract,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
    )

def calculate_contract_fingerprint(contract: dict) -> str:
    """
    Calculate SHA-256 fingerprint for a normalised contract.
    """

    normalised = normalise_contract(contract)

    return hashlib.sha256(
    normalised.encode("utf-8")
    ).hexdigest()

def validate_required_metadata(metadata: dict) -> None:
    """
    Validate that all required metadata fields exist and contain
    non-empty string values.
    """

    required_keys = [
        "source",
        "schema_name",
        "database_name",
        "table_name",
        "contract_version"
    ]

    missing_keys = [
        key for key in required_keys
        if key not in metadata
    ]

    if missing_keys:
        raise ValueError(
            f"Missing required metadata fields: {', '.join(missing_keys)}"
        )

    invalid_keys = [
        key for key in required_keys
        if metadata.get(key) is None
        or not isinstance(metadata.get(key), str)
        or not metadata.get(key).strip()
    ]

    if invalid_keys:
        raise ValueError(
            f"Required metadata fields must contain non-empty string values: "
            f"{', '.join(invalid_keys)}"
        )

def lambda_handler(event, context):
    try:
        # Loop is required because AWS passes S3 records inside a list wrapper
        for record in event['Records']:
            object_key = urllib.parse.unquote_plus(record['s3']['object']['key'])
            bucket_name = record['s3']['bucket']['name']

            logger.info("Schema registration started.",
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

            #validating metadata
            validate_required_metadata(metadata)
            
            logger.info("Reading metadata from schema ...")

            source_name = metadata["source"]
            schema_name = metadata["schema_name"]
            db_name = metadata["database_name"]
            table_name = metadata["table_name"]
            contract_version = metadata["contract_version"]
        

            contract_uri = f"s3://{bucket_name}/{object_key}"
           
            contract_id = str(uuid.uuid5(uuid.NAMESPACE_DNS,f"{object_key}"))

            event_time = datetime.now(timezone.utc).isoformat()

            logger.info(f"contract_uri  ::: {contract_uri} , contract_id :::: {contract_id}")

            ''' If write succeeds → new version.
                If condition fails → fetch existing item.
                    Same fingerprint → treat as already registered.
                    Different fingerprint → fail and require a new version.

                This pattern is idempotent and safe against duplicate S3 events.
            '''
            item={
                    
                    'contract_id': contract_id,
                    'ingestion_version':"",
                    'registered_at': event_time,
                    'contract_version': contract_version,
                    'source_name': source_name,
                    'schema_name': schema_name,
                    'db_name': db_name,
                    'table_name': table_name,
                    'schema_fingerprint': schema_fingerprint,
                    's3_contract_uri': contract_uri,
                    'is_active': False
                }
            
            audit_table.put_item(
                Item=item,
                ConditionExpression="""
                    attribute_not_exists(contract_id)
                    AND attribute_not_exists(contract_version)
                    """
            )

            logger.info(
                "New contract version registered",
                extra={
                "execution_id": context.aws_request_id,
                "contract_id": contract_id,
                "contract_version": contract_version
                }
            )

        return {
            'statusCode': 200,
            'body': json.dumps('Successfully entered the schema into the DynamoDB audit table.')
        }
        
    except ClientError as e:

        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            logger.exception(
                f"Unexpected DynamoDB error while registering contract {contract_id} version {contract_version}"
                )
            raise
        
        existing = audit_table.get_item(
            Key={
                "contract_id": contract_id,
                "contract_version": contract_version
                }
        ).get("Item")
        
        if existing["schema_fingerprint"] == schema_fingerprint:
            logger.info(
            "Contract version already registered with same fingerprint.",
             extra={
                "execution_id": context.aws_request_id,
                "contract_id": contract_id,
                "contract_version": contract_version
                }
            )
        
        else:

            suggested_version = increment_contract_version(contract_version)
            raise ValueError(
                f"Contract version '{contract_version}' already exists for contract_id '{contract_id}' but has a different "
                f"schema fingerprint. Please register a new contract version."
            )

