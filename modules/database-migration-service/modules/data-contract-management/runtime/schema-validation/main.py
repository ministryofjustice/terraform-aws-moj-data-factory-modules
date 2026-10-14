import hashlib
import os 
import uuid
import urllib.parse
import json
import boto3
import pyarrow as pa
import pyarrow.parquet as pq
import io
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
        raise ValueError(
            "Unsigned 64-bit integers cannot be represented "
            "safely as Avro long."
        )

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
        if field_type.unit == "ns":
            raise ValueError(
                "Nanosecond timestamps are not supported "
                "by the current schema comparison policy."
            )

        timestamp_mapping = {
            "us": "timestamp-micros",
            "ms": "timestamp-millis",
            "s": "timestamp-millis",
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
        element_field = field_type.value_field

        element_type = arrow_type_to_avro(
            element_field.type,
            f"{field_name or 'array'}_item",
        )

        if element_field.nullable and element_type != "null":
            element_type = ["null", element_type]

        return {
            "type": "array",
            "items": element_type,
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


class S3ObjectReader(io.RawIOBase):
    MAX_READ_BYTES = 64 * 1024 * 1024

    def __init__(
        self,
        bucket,
        key,
        version_id=None,
        expected_etag=None,
    ):
        super().__init__()

        self._request = {
            "Bucket": bucket,
            "Key": key,
        }

        if version_id is not None:
            self._request["VersionId"] = version_id

        if expected_etag:
            self._request["IfMatch"] = (
                '"' + expected_etag.strip('"') + '"'
            )

        response = s3.head_object(**self._request)

        self._size = response["ContentLength"]
        self._position = 0

        self._request["IfMatch"] = response["ETag"]

        resolved_version = response.get("VersionId")
        if resolved_version is not None:
            self._request["VersionId"] = resolved_version

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        self._checkClosed()
        return self._position

    def seek(self, offset, whence=io.SEEK_SET):
        self._checkClosed()

        if whence == io.SEEK_SET:
            position = offset
        elif whence == io.SEEK_CUR:
            position = self._position + offset
        elif whence == io.SEEK_END:
            position = self._size + offset
        else:
            raise ValueError(f"Unsupported seek mode: {whence}")

        if position < 0:
            raise ValueError("Cannot seek before the start of the object.")

        self._position = position
        return position

    def read(self, size=-1):
        self._checkClosed()

        remaining = max(0, self._size - self._position)

        if size is None or size < 0:
            size = remaining
        else:
            size = min(size, remaining)

        if size == 0:
            return b""

        if size > self.MAX_READ_BYTES:
            raise ValueError(
                "Parquet schema reading requested more than "
                "64 MiB in one read. This file requires review."
            )

        start = self._position
        end = start + size - 1

        response = s3.get_object(
            **self._request,
            Range=f"bytes={start}-{end}",
        )

        body = response["Body"]
        try:
            data = body.read(size)
        finally:
            body.close()

        if len(data) != size:
            raise IOError(
                "Incomplete S3 range read while reading Parquet schema."
            )

        self._position += len(data)
        return data

    def readinto(self, buffer):
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)


def read_parquet_schema_from_s3(
    bucket: str,
    key: str,
    version_id=None,
    expected_etag=None,
) -> pa.Schema:
    """Read Parquet metadata using a pinned seekable S3 object."""
    with S3ObjectReader(
        bucket=bucket,
        key=key,
        version_id=version_id,
        expected_etag=expected_etag,
    ) as reader:
        parquet_file = pq.ParquetFile(reader)
        return parquet_file.schema_arrow


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
    version_id=None,
    expected_etag=None,
) -> dict:
    """Generate a temporary Avro schema from the incoming Parquet file."""
    parquet_schema = read_parquet_schema_from_s3(
        bucket=bucket_name,
        key=object_key,
        version_id=version_id,
        expected_etag=expected_etag,
    )

    validate_avro_name(table_name, "Table")

    avro_schema = build_avro_schema(
        parquet_schema=parquet_schema,
        record_name=table_name,
        namespace="uk.gov.justice.validation",
    )

    logger.info(
        "Generated Avro schema for %s.%s.%s",
        database_name,
        schema_name,
        table_name,
    )

    return avro_schema

def resolve_source_metadata(bucket_name: str, object_key: str) -> dict:
    raw_mappings = os.environ.get("SOURCE_MAPPINGS")

    if not raw_mappings:
        raise ValueError(
            "SOURCE_MAPPINGS must contain the configured "
            "incoming S3 locations and contract identities."
        )

    try:
        mappings = json.loads(raw_mappings)
    except json.JSONDecodeError as error:
        raise ValueError(
            "SOURCE_MAPPINGS must be valid JSON."
        ) from error

    if not isinstance(mappings, list) or not mappings:
        raise ValueError(
            "SOURCE_MAPPINGS must be a non-empty list."
        )

    matches = []

    for mapping in mappings:
        if not isinstance(mapping, dict):
            raise ValueError(
                "Each source mapping must be an object."
            )

        validate_required_metadata(mapping)

        configured_bucket = mapping.get("bucket_name")
        prefix = mapping.get("prefix")

        if (
            not isinstance(configured_bucket, str)
            or not configured_bucket
            or configured_bucket != configured_bucket.strip()
        ):
            raise ValueError(
                "Each source mapping requires a valid bucket_name."
            )

        if (
            not isinstance(prefix, str)
            or not prefix
            or prefix != prefix.strip()
            or prefix.startswith("/")
            or not prefix.endswith("/")
        ):
            raise ValueError(
                "Each source mapping requires a non-empty prefix "
                "with no leading slash and a trailing slash."
            )

        if (
            bucket_name == configured_bucket
            and object_key.startswith(prefix)
            and len(object_key) > len(prefix)
        ):
            matches.append(mapping)

    if len(matches) != 1:
        raise ValueError(
            "Expected exactly one source mapping for "
            f"s3://{bucket_name}/{object_key}; "
            f"found {len(matches)}."
        )

    return {
        field: matches[0][field]
        for field in REQUIRED_METADATA
    }

def extract_version_number(contract_version: str) -> int:
    if (
        not isinstance(contract_version, str)
        or not re.fullmatch(r"v[1-9][0-9]*", contract_version)
    ):
        raise ValueError(
            "Invalid contract version. Expected v1, v2, etc., "
            "with no whitespace or leading zeros."
        )

    return int(contract_version[1:])


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


def canonicalise_type(avro_type):
    primitive_types = {
        "null",
        "boolean",
        "int",
        "long",
        "float",
        "double",
        "bytes",
        "string",
    }

    if isinstance(avro_type, list):
        non_null_types = [
            value for value in avro_type if value != "null"
        ]

        if (
            len(non_null_types) != 1
            or len(avro_type) != 2
            or "null" not in avro_type
        ):
            raise ValueError(
                f"Unsupported Avro union for comparison: {avro_type}"
            )

        return {
            "nullable": True,
            "value": canonicalise_type(non_null_types[0]),
        }

    if isinstance(avro_type, str):
        if avro_type not in primitive_types:
            raise ValueError(
                "Named Avro type references are not supported "
                f"for comparison yet: {avro_type}"
            )

        return {"type": avro_type}

    if not isinstance(avro_type, dict):
        raise ValueError(
            f"Unsupported Avro type for comparison: {avro_type}"
        )

    base_type = avro_type.get("type")

    if not isinstance(base_type, str):
        raise ValueError(
            f"Unsupported Avro type definition: {avro_type}"
        )

    result = {"type": base_type}

    if base_type == "array":
        result["items"] = canonicalise_type(avro_type["items"])
    elif base_type == "map":
        result["values"] = canonicalise_type(avro_type["values"])
    elif base_type == "record":
        result["fields"] = canonicalise_schema(avro_type)["fields"]
    elif base_type == "enum":
        result["symbols"] = list(avro_type["symbols"])
    elif base_type == "fixed":
        result["size"] = avro_type["size"]
    elif base_type not in primitive_types:
        raise ValueError(
            f"Unsupported Avro base type for comparison: {base_type}"
        )

    logical_type = avro_type.get("logicalType")

    if logical_type is not None:
        supported_logical_types = {
            "decimal",
            "date",
            "time-millis",
            "time-micros",
            "timestamp-millis",
            "timestamp-micros",
            "timestamp-nanos",
            "local-timestamp-millis",
            "local-timestamp-micros",
            "local-timestamp-nanos",
            "uuid",
            "duration",
        }

        if logical_type not in supported_logical_types:
            raise ValueError(
                "Unsupported logical type for comparison: "
                f"{logical_type}"
            )

        result["logical_type"] = logical_type

        if logical_type == "decimal":
            result["precision"] = avro_type["precision"]
            result["scale"] = avro_type.get("scale", 0)

    return result


def canonicalise_schema(schema: dict) -> dict:
    if not isinstance(schema, dict) or schema.get("type") != "record":
        raise ValueError(
            "Schema comparison requires an Avro record."
        )

    fields = schema.get("fields")

    if not isinstance(fields, list) or not fields:
        raise ValueError(
            "Schema comparison requires a non-empty fields list."
        )

    canonical_fields = []
    seen_names = set()

    for field in fields:
        if not isinstance(field, dict):
            raise ValueError("Each schema field must be an object.")

        field_name = field.get("name")
        validate_avro_name(field_name, "Field")

        if field_name in seen_names:
            raise ValueError(
                f"Duplicate field name: {field_name}"
            )

        seen_names.add(field_name)

        if "type" not in field:
            raise ValueError(
                f"Field '{field_name}' is missing its type."
            )

        canonical_fields.append(
            {
                "name": field_name,
                "type": canonicalise_type(field["type"]),
            }
        )

    return {"fields": canonical_fields}

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

def get_latest_contract(contract_id: str) -> dict | None:
    request = {
        "KeyConditionExpression": Key("contract_id").eq(contract_id),
        "ConsistentRead": True,
    }

    latest_contract = None
    highest_version = -1

    while True:
        response = schema_audit_table.query(**request)

        for item in response.get("Items", []):
            version_number = extract_version_number(
                item["contract_version"]
            )

            if version_number > highest_version:
                highest_version = version_number
                latest_contract = item

        last_key = response.get("LastEvaluatedKey")

        if not last_key:
            break

        request["ExclusiveStartKey"] = last_key

    if latest_contract is not None:
        logger.info(
            "Registered contract selected.",
            extra={
                "contract_id": contract_id,
                "contract_version": latest_contract["contract_version"],
                "selection_policy": "highest_registered_version",
            },
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

class SchemaValidationFailure(ValueError):
    def __init__(self, error_type, message):
        super().__init__(message)
        self.error_type = error_type


def build_validation_id(bucket, key, object_details):
    version_id = object_details.get("versionId")

    if version_id is not None:
        object_identity = {
            "version_id": version_id,
            "etag": object_details.get("eTag"),
            "sequencer": object_details.get("sequencer"),
        }
    else:
        etag = object_details.get("eTag")
        sequencer = object_details.get("sequencer")

        if not etag or not sequencer:
            raise ValueError(
                "An unversioned S3 event must include "
                "eTag and sequencer to identify the incoming object."
            )

        object_identity = {
            "etag": etag,
            "sequencer": sequencer,
        }

    payload = json.dumps(
        {
            "bucket": bucket,
            "key": key,
            "object": object_identity,
        },
        sort_keys=True,
        separators=(",", ":"),
    )

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_validation_result(item):
    validation_audit_table.put_item(Item=item)

def process_event(record, context):
    execution_id = getattr(context, "aws_request_id", None)
    log_context = {"execution_id": execution_id}
    item = None

    try:
        bucket_name = record["s3"]["bucket"]["name"]
        object_details = record["s3"]["object"]
        object_key = urllib.parse.unquote_plus(
            object_details["key"]
        )
        version_id = object_details.get("versionId")

        validation_id = build_validation_id(
            bucket_name,
            object_key,
            object_details,
        )

        item = {
            "validation_id": validation_id,
            "s3_object_uri": f"s3://{bucket_name}/{object_key}",
            "bucket_name": bucket_name,
            "object_key": object_key,
            "s3_object_version_id": version_id,
            "object_etag": object_details.get("eTag"),
            "object_sequencer": object_details.get("sequencer"),
            "ingestion_type": INGESTION_TYPE,
            "validation_execution_id": execution_id,
            "validation_timestamp": datetime.now(
                timezone.utc
            ).isoformat(),
            "comparison_policy": "field_structure_v1",
            "contract_selection_policy": (
                "highest_registered_version"
            ),
        }

        log_context.update(
            {
                "validation_id": validation_id,
                "bucket_name": bucket_name,
                "object_key": object_key,
                "s3_object_version_id": version_id,
            }
        )

        logger.info(
            "Schema validation started.",
            extra=log_context,
        )

        metadata = resolve_source_metadata(
            bucket_name,
            object_key,
        )
        validate_required_metadata(metadata)

        contract_id = build_contract_id(metadata)

        item.update({
            field: metadata[field]
            for field in REQUIRED_METADATA
        })
        item["contract_id"] = contract_id
        log_context["contract_id"] = contract_id

        latest_contract = get_latest_contract(contract_id)

        if latest_contract is None:
            raise SchemaValidationFailure(
                "CONTRACT_NOT_FOUND",
                f"No registered contract found for {contract_id}.",
            )

        contract_version = latest_contract["contract_version"]
        item["contract_version"] = contract_version

        registered_uri = latest_contract.get("s3_contract_uri")
        registered_bucket, registered_key = parse_s3_uri(
            registered_uri
        )

        registered_contract, registered_version_id = read_contract(
            registered_bucket,
            registered_key,
            latest_contract.get("s3_object_version_id"),
        )

        item["s3_contract_uri"] = registered_uri
        item["registered_contract_object_version_id"] = (
            registered_version_id
        )

        validate_required_metadata(registered_contract)
        validate_avro_schema(registered_contract)

        identity_mismatches = [
            field
            for field in REQUIRED_METADATA
            if registered_contract[field] != metadata[field]
        ]

        if (
            identity_mismatches
            or build_contract_id(registered_contract) != contract_id
            or latest_contract.get("contract_id") != contract_id
        ):
            raise SchemaValidationFailure(
                "CONTRACT_IDENTITY_MISMATCH",
                "The registered contract does not match "
                "the incoming source identity."
                + (
                    " Mismatched fields: "
                    + ", ".join(identity_mismatches)
                    if identity_mismatches
                    else ""
                ),
            )

        if (
            registered_contract.get("contract_version")
            != contract_version
        ):
            raise SchemaValidationFailure(
                "CONTRACT_VERSION_MISMATCH",
                "The loaded contract version does not match "
                "the selected registry record.",
            )

        incoming_schema = generate_avro_schema(
            bucket_name=bucket_name,
            object_key=object_key,
            source=metadata["source"],
            database_name=metadata["database_name"],
            schema_name=metadata["schema_name"],
            table_name=metadata["table_name"],
            version_id=version_id,
            expected_etag=object_details.get("eTag"),
        )

        validate_avro_schema(incoming_schema)

        incoming_fingerprint = generate_schema_fingerprint(
            incoming_schema
        )
        registered_fingerprint = generate_schema_fingerprint(
            registered_contract
        )

        item["schema_fingerprint"] = incoming_fingerprint
        item["registered_schema_fingerprint"] = (
            registered_fingerprint
        )

        if incoming_fingerprint != registered_fingerprint:
            differences = schema_diff(
                registered_contract,
                incoming_schema,
            )

            if not differences:
                differences = [
                    "Field order differs between the schemas."
                ]

            item["schema_differences"] = differences
            item["comparison_outcome"] = "SCHEMA_CHANGED"

            raise SchemaValidationFailure(
                "SCHEMA_MISMATCH",
                "Incoming schema does not match the "
                "selected registered contract.",
            )

        item["comparison_outcome"] = "NO_CHANGE"
        item["validation_status"] = "SUCCESS"
        item["validation_summary"] = "Schema matched."
        item["schema_differences"] = []

        write_validation_result(item)

        logger.info(
            "Schema validation successful.",
            extra={
                **log_context,
                "contract_version": contract_version,
            },
        )

        return {
            "validation_id": validation_id,
            "contract_id": contract_id,
            "contract_version": contract_version,
            "validation_status": "SUCCESS",
            "comparison_outcome": "NO_CHANGE",
            "bucket_name": bucket_name,
            "object_key": object_key,
            "s3_object_version_id": version_id,
            "schema_fingerprint": incoming_fingerprint,
            "registered_schema_fingerprint": registered_fingerprint,
            "s3_contract_uri": registered_uri,
            "registered_contract_object_version_id": (
                registered_version_id
            ),
        }

    except Exception as error:
        if item is not None:
            item["validation_status"] = "FAIL"
            item["validation_summary"] = str(error)
            item["error_type"] = getattr(
                error,
                "error_type",
                "VALIDATION_PROCESSING_ERROR",
            )
            item.setdefault(
                "comparison_outcome",
                "NOT_COMPLETED",
            )

            try:
                write_validation_result(item)
            except Exception:
                logger.exception(
                    "Could not persist the failed validation result.",
                    extra=log_context,
                )

        logger.exception(
            "Schema validation failed.",
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
    
# FULL and CDC both validate each incoming object.

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