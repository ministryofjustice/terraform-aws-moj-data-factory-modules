import hashlib
import os 
import uuid
import urllib.parse
import json
import boto3
import pyarrow as pa
import pyarrow.parquet as pq

from datetime import datetime, timezone
from boto3.dynamodb.conditions import Key
from typing import Any, Dict
from io import BytesIO

from logger import get_logger


logger = get_logger("schema-validation")

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
AUDIT_TABLE_NAME = os.environ["AUDIT_TABLE_NAME"]
INGESTION_TYPE = os.environ["INGESTION_TYPE"] #FULL or CDC 

if not AUDIT_TABLE_NAME.strip():
    raise ValueError("AUDIT_TABLE_NAME must not be empty.")
audit_table = dynamodb.Table(AUDIT_TABLE_NAME)

REQUIRED_METADATA = (
    "source",
    "database_name",
    "schema_name",
    "table_name",
)

def validate_required_metadata(metadata):
    if not isinstance(metadata, dict):
        raise ValueError("Contract must be a JSON object.")

    invalid_fields = [
        field
        for field in REQUIRED_METADATA
        if not isinstance(metadata.get(field), str)
        or not metadata[field].strip()
    ]

    if invalid_fields:
        raise ValueError(
            "Required metadata must contain non-empty strings: "
            + ", ".join(invalid_fields)
        )

    for field in REQUIRED_METADATA:
        if metadata[field] != metadata[field].strip():
            raise ValueError(
                f"Metadata field '{field}' must not contain "
                "leading or trailing whitespace."
            )

    for field in ("source", "database_name", "schema_name", "table_name"):
        if ":" in metadata[field]:
            raise ValueError(
                f"Metadata field '{field}' must not contain ':', "
                "because it separates contract identity fields."
            )

def build_contract_id(contract):
    identity = (
        f"{contract['source']}:"
        f"{contract['database_name']}:"
        f"{contract['schema_name']}:"
        f"{contract['table_name']}"
    )
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, identity))

def arrow_type_to_avro(
    field_type: pa.DataType,
) -> Any:
    """
    Convert PyArrow type to Avro type.
    """

    if pa.types.is_null(field_type):
        return "null"

    if pa.types.is_boolean(field_type):
        return "boolean"

    if pa.types.is_int8(field_type):
        return "int"

    if pa.types.is_int16(field_type):
        return "int"

    if pa.types.is_int32(field_type):
        return "int"

    if pa.types.is_int64(field_type):
        return "long"

    if pa.types.is_uint8(field_type):
        return "int"

    if pa.types.is_uint16(field_type):
        return "int"

    if pa.types.is_uint32(field_type):
        return "long"

    if pa.types.is_uint64(field_type):
        return "long"

    if pa.types.is_float32(field_type):
        return "float"

    if pa.types.is_float64(field_type):
        return "double"

    if pa.types.is_string(field_type):
        return "string"

    if pa.types.is_large_string(field_type):
        return "string"

    if pa.types.is_binary(field_type):
        return "bytes"

    if pa.types.is_large_binary(field_type):
        return "bytes"

    if pa.types.is_date(field_type):
        return {
            "type": "int",
            "logicalType": "date",
        }

    if pa.types.is_timestamp(field_type):
        return {
            "type": "long",
            "logicalType": "timestamp-millis",
        }

    if pa.types.is_decimal(field_type):
        return {
            "type": "bytes",
            "logicalType": "decimal",
            "precision": field_type.precision,
            "scale": field_type.scale,
        }

    if pa.types.is_list(field_type):
        return {
            "type": "array",
            "items": arrow_type_to_avro(
                field_type.value_type
            ),
        }

    if pa.types.is_struct(field_type):

        fields = []

        for nested_field in field_type:

            fields.append(
                {
                    "name": nested_field.name,
                    "type": [
                        "null",
                        arrow_type_to_avro(
                            nested_field.type
                        ),
                    ],
                    "default": None,
                }
            )

        return {
            "type": "record",
            "name": "NestedRecord",
            "fields": fields,
        }

    raise ValueError(
        f"Unsupported Arrow type: {field_type}"
    )

def build_avro_schema(
    parquet_schema: pa.Schema,
    record_name: str,
    namespace: str,
) -> Dict[str, Any]:
    """
    Create Avro schema from parquet schema.
    """

    fields = []

    for field in parquet_schema:

        fields.append(
            {
                "name": field.name,
                "type": [
                    "null",
                    arrow_type_to_avro(field.type),
                ],
                "default": None,
            }
        )

    return {
        "type": "record",
        "name": record_name,
        "namespace": namespace,
        "fields": fields,
    }


def read_parquet_schema_from_s3(
    bucket: str,
    key: str,
) -> pa.Schema:
    """
    Read schema only from parquet footer.
    """

    obj = s3.get_object(
        Bucket=bucket,
        Key=key,
    )

    body = obj["Body"]

    try:

        parquet_bytes = body.read()

    finally:

        body.close()

    parquet_file = pq.ParquetFile(
        BytesIO(parquet_bytes)
    )

    return parquet_file.schema_arrow

"""
#TB used for FULL load ..yest to be implemented.
def list_parquet_files(
    bucket: str,
    prefix: str,
) -> list[str]:
   
    keys = []

    paginator = s3.get_paginator(
        "list_objects_v2"
    )

    for page in paginator.paginate(
        Bucket=bucket,
        Prefix=prefix,
    ):

        for item in page.get("Contents", []):

            key = item["Key"]

            if key.endswith(".parquet"):
                keys.append(key)

    return keys
"""


def normalise_contract(contract):
    return json.dumps(
        contract,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def generate_fingerprint(contract):
    """Hash the complete contract for duplicate/conflict detection."""
    normalised = normalise_contract(contract)
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def generate_avro_schema(
    bucket_name: str,
    object_key: str,
    source: str,
    database_name: str,
    schema_name: str,
    table_name: str,
) -> dict:
    """
    Generate Avro schema from Parquet file.
    """

    parquet_schema = (
        read_parquet_schema_from_s3(
            bucket=bucket_name,
            key=object_key,
        )
    )

    avro_schema = build_avro_schema(
        parquet_schema=parquet_schema,
        record_name=table_name,
        namespace=(
            f"{source}."
            f"{database_name}."
            f"{schema_name}"
        ),
    )

    logger.info(
        "Generated Avro schema "
        "for %s.%s.%s",
        database_name,
        schema_name,
        table_name,
    )

    return avro_schema

def extract_metadata_from_key(
    object_key: str,
) -> dict:

    parts = object_key.split("/")

    if len(parts) < 6:
        raise ValueError(
            f"Unexpected key structure: {object_key}"
        )

    return {
        "source": parts[1],
        "database_name": parts[2],
        "schema_name": parts[3],
        "table_name": parts[4],
        "prefix": "/".join(parts[:-1]),
    }

def get_latest_contract(
    contract_id: str,
) -> dict | None:
    """
    Return latest registered contract.
    """

    response = audit_table.query(
        KeyConditionExpression=
            Key("contract_id").eq(contract_id),
        ScanIndexForward=False,
        Limit=1,
        ConsistentRead=True,
    )

    items = response.get("Items", [])

    if not items:
        return None

    return items[0]

def process_event(record, context):
    log_context = {
        "execution_id": getattr(context, "aws_request_id", None),
    }

    try:
        bucket_name = record["s3"]["bucket"]["name"]
        object_details = record["s3"]["object"]
        object_key = urllib.parse.unquote_plus(object_details["key"])
        version_id = object_details.get("versionId")

        log_context.update(
            {
                "bucket_name": bucket_name,
                "object_key": object_key,
                "s3_object_version_id": version_id,
            }
        )

        logger.info("Schema Validation started.", extra=log_context)

        #extract metadata
        metadata = extract_metadata_from_key(
            object_key,
        )

        source = metadata["source"]
        database_name = metadata["database_name"]
        schema_name = metadata["schema_name"]
        table_name = metadata["table_name"]
        
        #Validate metadata
        validate_required_metadata(metadata)

        #Build contract_id
        contract_id = build_contract_id(metadata)

        #generate contarct from S3
        contract = generate_avro_schema(
            bucket_name=bucket_name,
            object_key=object_key,
            source=source,
            database_name=database_name,
            schema_name=schema_name,
            table_name=table_name,
        )

        logger.info(
            json.dumps(
                contract,
                indent=2,
            )
        )

        #generate finger print for NEW incoming data
        new_fingerprint = generate_fingerprint(contract)

        #Retrieve existing contract_id and schema finger print from dynamodb
        latest_contract = get_latest_contract(
            contract_id
        )
        if latest_contract is None:
            raise ValueError(
                f"No contract found for {contract_id}"
            )
        existing_fingerprint = (
            latest_contract.get(
                "schema_fingerprint"
            )
        )
        
        if not existing_fingerprint:
            raise ValueError(
                f"schema_fingerprint not found for "
                f"contract_id={contract_id}"
            )

        if (
            existing_fingerprint != new_fingerprint
        ):
            logger.error(
                "Schema validation failed. "
                "Contract fingerprint mismatch. "
                "contract_id=%s, version=%s, "
                "expected=%s, actual=%s",
                contract_id,
                latest_contract["contract_version"],
                existing_fingerprint,
                new_fingerprint,
            )

            raise ValueError(
                "Incoming schema does not match the latest "
                f"registered contract. Expected fingerprint "
                f"'{existing_fingerprint}' but received "
                f"'{new_fingerprint}'."
            )
        contract_version = latest_contract["contract_version"]
        ingestion_version = f"ING_{contract_version}"
        timestamp = datetime.now(
                timezone.utc
        ).isoformat()
        audit_table.update_item(
            Key={
                "contract_id": contract_id,
                "contract_version": contract_version,
            },
            UpdateExpression=(
                "SET "
                "ingestion_version = :ingestion_version, "
                "validation_status = :status, "
                "validation_summary = :summary, "
                "last_validated_at = :ts"
            ),
            ExpressionAttributeValues={
                ":ingestion_version": ingestion_version,
                ":status": "SUCCESS",
                ":summary": (
                    "schema macthed."
                ),
                ":ts": timestamp,
            },
        )

        logger.info(
            "Schema validation successful. "
            "contract_id=%s version=%s",
            contract_id,
            contract_version,
        )
        return {
            "contract_id": contract_id, 
            "contract_version": contract_version,
            "ingestion_version": ingestion_version,
            "validation_status": "SUCCESS",
        }
    except Exception:
        logger.exception(
            "Schema Validation failed.",
            extra=log_context,
        )
        raise


def lambda_handler(event, context):

    if not isinstance(event, dict):
        raise ValueError("Expected an S3 event object.")

    records = event.get("Records", [])

    if not isinstance(records, list) or not records:
        raise ValueError(
            "Expected an S3 event containing a non-empty Records list."
        )
    
    #Full Load 


    #CDC flow
    results = []
    for record in records:
        try:
            result = process_event(
                record,
                context,
            )

            results.append(result)

        except Exception as error:

            logger.exception(
                "Contract validation failed. "
                "Stopping further processing."
            )

            raise RuntimeError(
                f"Processing stopped because "
                f"contract validation failed: {error}"
            ) from error

    return {
        "statusCode": 200,
        "processed_records": len(results),
        "results": results,
    }