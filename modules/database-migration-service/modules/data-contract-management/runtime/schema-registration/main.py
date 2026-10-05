"""Register contracts that have passed upstream validation.

This module does not perform AVRO compatibility checks, allocate versions
or activate contracts. The deployment/workflow must ensure that only
permitted contracts reach registration.
"""

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


logger = get_logger("schema-registration")

AUDIT_TABLE_NAME = os.environ["AUDIT_TABLE_NAME"]

if not AUDIT_TABLE_NAME.strip():
    raise ValueError("AUDIT_TABLE_NAME must not be empty.")

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
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


def build_contract_id(contract):
    identity = (
        f"{contract['source']}:"
        f"{contract['database_name']}:"
        f"{contract['schema_name']}:"
        f"{contract['table_name']}"
    )
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, identity))


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


def register_contract(item):
    try:
        audit_table.put_item(
            Item=item,
            ConditionExpression=(
                "attribute_not_exists(contract_id) "
                "AND attribute_not_exists(contract_version)"
            ),
        )
        return "REGISTERED", item

    except ClientError as error:
        if (
            error.response["Error"]["Code"]
            != "ConditionalCheckFailedException"
        ):
            raise

        response = audit_table.get_item(
            Key={
                "contract_id": item["contract_id"],
                "contract_version": item["contract_version"],
            },
            ConsistentRead=True,
        )
        existing = response.get("Item")

        if existing is None:
            raise RuntimeError(
                "Registration write was rejected, but the existing "
                "record could not be found. Retry the operation."
            ) from error

        if not existing.get("schema_fingerprint"):
            raise RuntimeError(
                "Existing registration has no schema_fingerprint. "
                "Cannot safely confirm a duplicate."
            ) from error

        if (
            existing["schema_fingerprint"]
            != item["schema_fingerprint"]
        ):
            raise ValueError(
                f"Contract {item['contract_id']} version "
                f"{item['contract_version']} is already registered "
                "with different content. Use a new version allocated "
                "by the contract lifecycle process."
            ) from error

        return "ALREADY_REGISTERED", existing


def build_result(status, registered, contract):
    result = {
        "status": status,
        "contract_id": registered["contract_id"],
        "contract_version": registered["contract_version"],
        "schema_fingerprint": registered["schema_fingerprint"],
        "s3_contract_uri": registered["s3_contract_uri"],
    }

    for field in ("source", "database_name", "schema_name", "table_name"):
        result[field] = contract[field]

    if "s3_object_version_id" in registered:
        result["s3_object_version_id"] = registered[
            "s3_object_version_id"
        ]

    return result


def process_record(record, context):
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

        logger.info("Schema registration started.", extra=log_context)

        contract, object_version_id = read_contract(
            bucket_name,
            object_key,
            version_id,
        )
        validate_required_metadata(contract)

        contract_id = build_contract_id(contract)
        fingerprint = calculate_contract_fingerprint(contract)
        contract_uri = f"s3://{bucket_name}/{object_key}"

        log_context.update(
            {
                "contract_id": contract_id,
                "contract_version": contract["contract_version"],
                "s3_object_version_id": object_version_id,
            }
        )

        item = {
            "contract_id": contract_id,
            "contract_version": contract["contract_version"],
            "source": contract["source"],
            "database_name": contract["database_name"],
            "schema_name": contract["schema_name"],
            "table_name": contract["table_name"],
            "schema_fingerprint": fingerprint,
            "s3_contract_uri": contract_uri,
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "registration_execution_id": log_context["execution_id"],
            "ingestion_version": None,
            "is_active": False,
        }

        if object_version_id is not None:
            item["s3_object_version_id"] = object_version_id

        status, registered = register_contract(item)

        logger.info(
            "Schema registration completed.",
            extra={**log_context, "registration_status": status},
        )

        return build_result(status, registered, contract)

    except Exception:
        logger.exception(
            "Schema registration failed.",
            extra=log_context,
        )
        raise


def lambda_handler(event, context):
    if not isinstance(event, dict):
        raise ValueError("Expected an S3 event object.")

    records = event.get("Records")

    if not isinstance(records, list) or not records:
        raise ValueError(
            "Expected an S3 event containing a non-empty Records list."
        )

    results = []
    for record in records:
        results.append(process_record(record, context))

    return {
        "statusCode": 200,
        "results": results,
    }
