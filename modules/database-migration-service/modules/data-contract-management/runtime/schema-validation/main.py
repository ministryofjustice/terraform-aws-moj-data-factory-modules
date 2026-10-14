import hashlib
import os 
import uuid
import urllib.parse
import json
import boto3
import pyarrow as pa
import pyarrow.parquet as pq
import pyarrow.fs
import re

from datetime import datetime, timezone
from boto3.dynamodb.conditions import Key
from typing import Any, Dict
from fastavro.schema import parse_schema


from logger import get_logger


logger = get_logger("schema-validation")

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")

VALIDATION_AUDIT_TABLE_NAME = os.environ["VALIDATION_AUDIT_TABLE_NAME"]
SCHEMA_AUDIT_TABLE_NAME= os.environ["SCHEMA_AUDIT_TABLE_NAME"]
INGESTION_TYPE = os.environ["INGESTION_TYPE"] #FULL or CDC 

for table_name, value in {
    "VALIDATION_AUDIT_TABLE_NAME": VALIDATION_AUDIT_TABLE_NAME,
    "SCHEMA_AUDIT_TABLE_NAME": SCHEMA_AUDIT_TABLE_NAME,
}.items():

    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{table_name} must not be empty."
        )

    if value != value.strip():
        raise ValueError(
            f"{table_name} must not contain leading "
            f"or trailing whitespace."
        )

if INGESTION_TYPE not in {
    "FULL",
    "CDC",
}:
    raise ValueError(
        "INGESTION_TYPE must be FULL or CDC"
    )


REQUIRED_METADATA = (
    "source",
    "database_name",
    "schema_name",
    "table_name",
)

schema_audit_table = dynamodb.Table(SCHEMA_AUDIT_TABLE_NAME)
validation_audit_table = dynamodb.Table(VALIDATION_AUDIT_TABLE_NAME)

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

    logger.info(
        "Contract identity string: '%s'",
        identity,
    )
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, identity))

def arrow_type_to_avro(
    field_type: pa.DataType,
    field_name: str | None = None,
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

        timestamp_mapping = {
            "us": "timestamp-micros",
            "ms": "timestamp-millis",
            "s": "timestamp-millis",
            "ns": "timestamp-micros",
        }

        logical_type = timestamp_mapping.get(
            field_type.unit
        )

        if logical_type is None:
            raise ValueError(
                f"Unsupported timestamp unit: "
                f"{field_type.unit}"
            )

        return {
            "type": "long",
            "logicalType": logical_type,
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
                build_avro_field(
                    nested_field
                )
            )

        return {
            "type": "record",
            "name": f"{field_name}_record",
            "fields": fields,
        }

    raise ValueError(
        f"Unsupported Arrow type: {field_type}"
    )

def build_avro_field(
    field: pa.Field,
) -> dict:
    """
    Build Avro field preserving nullability.
    """

    validate_avro_name(
        field.name,
        "Field",
    )

    avro_type = arrow_type_to_avro(
        field.type,
        field.name,
    )

    # Special case: Arrow null type
    if avro_type == "null":
        return {
            "name": field.name,
            "type": "null",
        }

    # Nullable column
    if field.nullable:
        return {
            "name": field.name,
            "type": [
                "null",
                avro_type,
            ],
            "default": None,
        }

    # Required column
    return {
        "name": field.name,
        "type": avro_type,
    }

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
            build_avro_field(field)
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

    s3fs = pyarrow.fs.S3FileSystem()

    with s3fs.open_input_file(
        f"{bucket}/{key}"
    ) as f:

        parquet_file = pq.ParquetFile(f)

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



def validate_avro_name(value, label):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value)
    ):
        raise ValueError(
            f"{label} '{value}' is not a valid Avro name. "
            "Use a letter or underscore first, followed by "
            "letters, numbers or underscores. "
            "Source identifiers requiring renaming are not "
            "supported yet."
        )
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

    validate_avro_name(
        source,
        "Source",
    )
    validate_avro_name(
        database_name,
        "Database",
    )
    validate_avro_name(
        schema_name,
        "Schema",
    )
    validate_avro_name(
        table_name,
        "Table",
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

    if len(parts) < 5:
        raise ValueError(
            f"Unexpected object key format: {object_key}"
        )

    return {
        "source": parts[0],
        "database_name": parts[1],
        "schema_name": parts[2],
        "table_name": parts[3],
        "prefix": "/".join(parts[:-1]),
    }

def extract_version_number(
    contract_version: str,
) -> int:

    if not isinstance(contract_version, str):
        raise ValueError(
            f"Invalid contract version: "
            f"{contract_version}"
        )

    match = re.fullmatch(
        r"v(\d+)",
        contract_version.strip(),
    )

    if not match:
        raise ValueError(
            f"Invalid contract version format: "
            f"{contract_version}. "
            f"Expected format 'v<number>'."
        )

    return int(match.group(1))



def validate_generated_contract(
    contract: dict,
) -> None:
    """
    Validate generated Avro contract before
    schema fingerprint generation.
    """

    if not isinstance(contract, dict):
        raise ValueError(
            "Generated contract must be a JSON object."
        )

    if contract.get("type") != "record":
        raise ValueError(
            "Generated schema type must be 'record'."
        )

    record_name = contract.get("name")

    validate_avro_name(
        record_name,
        "Record name",
    )

    namespace = contract.get("namespace")

    if (
        not isinstance(namespace, str)
        or not namespace.strip()
    ):
        raise ValueError(
            "Generated schema namespace is missing."
        )

    for part in namespace.split("."):
        validate_avro_name(
            part,
            "Namespace segment",
        )

    fields = contract.get("fields")

    if not isinstance(fields, list):
        raise ValueError(
            "Generated schema fields must be a list."
        )

    if not fields:
        raise ValueError(
            "Generated schema contains no fields."
        )

    seen_names = set()

    for field in fields:

        if not isinstance(field, dict):
            raise ValueError(
                "Each field must be a JSON object."
            )

        field_name = field.get("name")

        validate_avro_name(
            field_name,
            "Field name",
        )

        if field_name in seen_names:
            raise ValueError(
                f"Duplicate field name "
                f"'{field_name}'."
            )

        seen_names.add(field_name)

        if "type" not in field:
            raise ValueError(
                f"Field '{field_name}' "
                f"is missing a type."
            )

def validate_avro_schema(
    schema: dict,
) -> None:
    try:
        validate_generated_contract(schema)
        parse_schema(schema)

    except Exception as exc:
        raise ValueError(
            f"Generated schema is not a valid "
            f"Avro schema: {exc}"
        ) from exc


def canonicalise_type(
    avro_type,
):
    nullable = False

    if (
        isinstance(avro_type, list)
        and "null" in avro_type
    ):
        nullable = True

        non_null_types = [
            t
            for t in avro_type
            if t != "null"
        ]

        if len(non_null_types) != 1:
            raise ValueError(
                f"Unsupported Avro union type: "
                f"{avro_type}"
            )

        avro_type = non_null_types[0]

    if isinstance(avro_type, dict):

        return (
            avro_type["type"],
            avro_type.get(
                "logicalType"
            ),
            nullable,
        )

    return (
        avro_type,
        None,
        nullable,
    )


def canonicalise_schema(
    schema: dict,
) -> dict:
    """
    Produces a canonical schema representation.

    Keeps:
        field name
        field type
        logical type
        nullability

    Ignores:
        namespace
        service
        source
        description
        contacts
        default
        key
        version
    """

    canonical_fields = []

    for field in schema["fields"]:

        (
            base_type,
            logical_type,
            nullable,
        ) = canonicalise_type(
            field["type"]
        )

        canonical_fields.append(
            {
                "name": field["name"],
                "type": base_type,
                "logical_type": logical_type,
                "nullable": nullable,
            }
        )

    return {
        "fields": canonical_fields
    }

def generate_schema_fingerprint(
    schema: dict,
) -> str:

    canonical_schema = (
        canonicalise_schema(
            schema
        )
    )

    payload = json.dumps(
        canonical_schema,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )

    logger.info(
        "Canonical schema used for fingerprint generation: %s",
        json.dumps(
            canonical_schema,
            indent=2,
            sort_keys=True,
        ),
    )

    return hashlib.sha256(
        payload.encode("utf-8")
    ).hexdigest()

def compare_schemas(
    registered_schema: dict,
    generated_schema: dict,
) -> bool:

    return (
        canonicalise_schema(
            registered_schema
        )
        ==
        canonicalise_schema(
            generated_schema
        )
    )



def schema_diff(
    registered_schema: dict,
    generated_schema: dict,
) -> list[str]:

    expected_fields = (
        canonicalise_schema(
            registered_schema
        )["fields"]
    )

    actual_fields = (
        canonicalise_schema(
            generated_schema
        )["fields"]
    )

    differences = []

    expected_map = {
        field["name"]: field
        for field in expected_fields
    }

    actual_map = {
        field["name"]: field
        for field in actual_fields
    }

    expected_names = set(
        expected_map.keys()
    )

    actual_names = set(
        actual_map.keys()
    )

    for field in sorted(
        expected_names - actual_names
    ):
        differences.append(
            f"Missing field '{field}'"
        )

    for field in sorted(
        actual_names - expected_names
    ):
        differences.append(
            f"Unexpected field '{field}'"
        )

    for field_name in sorted(
        expected_names & actual_names
    ):

        expected = expected_map[field_name]
        actual = actual_map[field_name]

        if expected != actual:

            differences.append(
                f"Field '{field_name}' mismatch. "
                f"Expected={expected}, "
                f"Actual={actual}"
            )

    return differences

def get_latest_contract(
    contract_id: str,
) -> dict | None:
    """
    Returns the contract with the highest
    numeric contract version.

    Example:
        v1, v2, v9, v10 -> returns v10
    """

    response = schema_audit_table.query(
        KeyConditionExpression=
            Key("contract_id").eq(contract_id),
        ConsistentRead=True,
    )

    items = response.get("Items", [])

    if not items:
        return None

    latest_contract = max(
        items,
        key=lambda item: extract_version_number(
            item["contract_version"]
        ),
    )

    logger.info(
        "Latest contract selected. "
        "contract_id=%s, "
        "contract_version=%s",
        contract_id,
        latest_contract["contract_version"],
    )

    return latest_contract

def read_contract(bucket_name, object_key, version_id=None):
    request = {
        "Bucket": bucket_name,
        "Key": object_key,
    }

    if version_id is not None:
        request["VersionId"] = version_id

    response = s3.get_object(**request)
    body = response["Body"]

    try:
        contract = json.loads(body.read().decode("utf-8"))
    finally:
        body.close()

    return contract, response.get("VersionId", version_id)


from urllib.parse import urlparse


def parse_s3_uri(s3_uri: str) -> tuple[str, str]:

    if not isinstance(s3_uri, str) or not s3_uri.strip():
        raise ValueError(
            "s3_uri must be a non-empty string."
        )

    parsed = urlparse(s3_uri)

    if parsed.scheme != "s3":
        raise ValueError(
            f"Invalid S3 URI: {s3_uri}"
        )

    if not parsed.netloc:
        raise ValueError(
            f"Missing bucket name in S3 URI: {s3_uri}"
        )

    object_key = parsed.path.lstrip("/")

    if not object_key:
        raise ValueError(
            f"Missing object key in S3 URI: {s3_uri}"
        )

    return (
        parsed.netloc,
        object_key,
    )



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

        logger.info(f"source -->{source}, database_name---->{database_name}, schema_name ---------->{schema_name} , table_name --------{table_name}")
        logger.info(f"contract_id ------->{contract_id}")
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
        
        validate_avro_schema(contract)

        #Retrieve existing contract_id and schema finger print from dynamodb
        latest_contract = get_latest_contract(
            contract_id
        )
        if latest_contract is None:
            error_details = {
                "error_type": "CONTRACT_NOT_FOUND",
                "contract_id": contract_id,
                "source": source,
                "bucket_name": bucket_name,
                "object_key": object_key,
                "object_version_id": version_id,
            }

            logger.warning(
                "No registered contract found.",
                extra=error_details,
            )
            raise ValueError(
                f"No contract found for {contract_id}"
            )
        # Get S3 path from Dynamo db passing contract_Id and fetch latest record

        registered_s3_path = (
            latest_contract.get(
                "s3_contract_uri"
            )
        )
        logger.info(f"registered_s3_path ------>{registered_s3_path}")
        # Read registred contract from S3
        registered_bucket , registered_object_key = parse_s3_uri(registered_s3_path)
        registered_contract, regis_object_version_id = read_contract(
            registered_bucket,
            registered_object_key,
        )

        logger.info(
            "Registered contract location. "
            "bucket=%s key=%s",
            registered_bucket,
            registered_object_key,
        )

        # Convert canonical form
        '''
        schemas_match = compare_schemas(
            registered_contract,
            contract,
        )
        '''
        new_fingerprint = (
            generate_schema_fingerprint(
                contract
            )
        )

        registered_fingerprint = (
            generate_schema_fingerprint(
                registered_contract
            )
        )

        #if not schemas_match: - to be added for logging.error - TO BE WORKED ON
        '''
        logger.info(
            "Registered contract loaded: %s",
            json.dumps(
                registered_contract,
                indent=2,
                sort_keys=True,
            ),
        )

        logger.info(
            "Generated contract from parquet: %s",
            json.dumps(
                contract,
                indent=2,
                sort_keys=True,
            ),
        )
        logger.info(
            "Schema comparison result. "
            "schemas_match=%s, "
            "registered_fingerprint=%s, "
            "generated_fingerprint=%s",
            schemas_match,
            registered_fingerprint,
            new_fingerprint,
        )

        logger.info(
            "Registered canonical schema: %s",
            json.dumps(
                canonicalise_schema(
                    registered_contract
                ),
                indent=2,
                sort_keys=True,
            ),
        )

        logger.info(
            "Generated canonical schema: %s",
            json.dumps(
                canonicalise_schema(
                    contract
                ),
                indent=2,
                sort_keys=True,
            ),
        )
        '''
        contract_version = registered_contract["contract_version"]
        ingestion_version = f"ING_{contract_version}"
        timestamp = datetime.now(
                timezone.utc
        ).isoformat()


        item = {
            "contract_id": contract_id,
            "contract_version": contract_version,
            "source": source,
            "database_name": database_name,
            "schema_name": schema_name,
            "table_name": table_name,
            "schema_fingerprint": new_fingerprint,
            "s3_object_uri": (
                f"s3://{bucket_name}/{object_key}"
            ),
            "s3_object_version_id": version_id,
            "validation_timestamp": timestamp,
            "ingestion_version": (
                f"ING_{contract_version}"
            ),
            
        }
        
        if not registered_fingerprint:
            raise ValueError(
                f"schema_fingerprint not found for "
                f"contract_id={contract_id}"
            )

        if (registered_fingerprint != new_fingerprint):
            item["validation_status"] = "FAIL"
            item["validation_summary"] = "Schema not matched"
            item["registered_schema_fingerprint"] = registered_fingerprint

            validation_audit_table.put_item(
                Item=item
            )

            logger.error(
                "Schema validation failed. "
                "Contract fingerprint mismatch. "
                "contract_id=%s, version=%s, "
                "expected=%s, actual=%s",
                contract_id,
                latest_contract["contract_version"],
                registered_fingerprint,
                new_fingerprint,
            )

            raise ValueError(
                "Incoming schema does not match the latest "
                f"registered contract. Expected fingerprint "
                f"'{registered_fingerprint}' but received "
                f"'{new_fingerprint}'."
            )
        
        item["validation_status"] = "SUCCESS"
        item["validation_summary"] = "Schema matched"

        validation_audit_table.put_item(
            Item=item
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