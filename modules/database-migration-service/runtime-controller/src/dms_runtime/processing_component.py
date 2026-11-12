import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any
from uuid import uuid4

from botocore.exceptions import ClientError

from ingestion_registry.references import S3ObjectReference


MAX_REQUEST_BYTES = 128 * 1024
DIGEST = re.compile(r"[0-9a-f]{64}")


class ProcessingOperationConflict(RuntimeError):
    """The operation already exists with different inputs or state."""


def _require_digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _encode_request(request: dict[str, Any]) -> bytes:
    encoded = json.dumps(
        request,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError("Processing request exceeds the 128 KiB limit")
    return encoded


def _conditional_failure(error: ClientError) -> bool:
    return (
        error.response.get("Error", {}).get("Code")
        == "ConditionalCheckFailedException"
    )


class ProcessingComponent:
    def __init__(self, table: Any) -> None:
        self._table = table

    def handle(self, event: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(event, Mapping):
            raise ValueError("Expected a Processing request object")

        version = event.get("request_version")
        if type(version) is not int or version != 1:
            raise ValueError("request_version must be the integer 1")

        operation_id = _require_digest(event.get("operation_id"), "operation_id")
        action = event.get("action")
        fields = {"request_version", "action", "operation_id"}

        if action == "lookup_processing":
            if set(event) != fields:
                raise ValueError("Unexpected lookup_processing fields")
            item = self._read(operation_id)
            if item is None:
                return {
                    "response_version": 1,
                    "operation_id": operation_id,
                    "status": "NOT_FOUND",
                }
            return self._response(item)

        if action == "submit_processing":
            if set(event) != fields | {"request_fingerprint", "request"}:
                raise ValueError("Unexpected submit_processing fields")
            fingerprint = _require_digest(
                event["request_fingerprint"], "request_fingerprint"
            )
            return self._submit(operation_id, fingerprint, event["request"])

        raise ValueError("Unsupported Processing action")

    def _submit(
        self,
        operation_id: str,
        fingerprint: str,
        request: Any,
    ) -> dict[str, Any]:
        if not isinstance(request, dict) or set(request) != {
            "ingestion_id",
            "deployment",
            "contract",
            "raw_batch",
        }:
            raise ValueError("Invalid Processing request fields")

        ingestion_id = request["ingestion_id"]
        if (
            not isinstance(ingestion_id, str)
            or not ingestion_id
            or ingestion_id != ingestion_id.strip()
            or len(ingestion_id.encode("utf-8")) > 256
        ):
            raise ValueError("Invalid Processing ingestion_id")

        expected_operation_id = hashlib.sha256(
            _encode_request(
                {"ingestion_id": ingestion_id, "stage": "PROCESSING"}
            )
        ).hexdigest()
        if operation_id != expected_operation_id:
            raise ValueError("operation_id does not match the ingestion")

        for field in ("deployment", "contract", "raw_batch"):
            if not isinstance(request[field], dict) or not request[field]:
                raise ValueError(f"{field} must be a non-empty object")

        try:
            S3ObjectReference(**request["raw_batch"])
            contract = request["contract"]
            if contract["kind"] != "registered-contract":
                raise ValueError("Expected a registered contract")
            S3ObjectReference(
                bucket_name=contract["contract_bucket_name"],
                object_key=contract["contract_object_key"],
                version_id=contract["contract_object_version_id"],
                sha256=contract["contract_object_sha256"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(
                "Processing requires versioned Raw batch and contract references"
            ) from error

        encoded = _encode_request(request)
        if hashlib.sha256(encoded).hexdigest() != fingerprint:
            raise ValueError("request_fingerprint does not match the request")

        item = {
            "operation_id": operation_id,
            "ingestion_id": ingestion_id,
            "request_fingerprint": fingerprint,
            "request_json": encoded.decode("utf-8"),
            "status": "ACCEPTED",
        }
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(operation_id)",
            )
        except ClientError as error:
            if not _conditional_failure(error):
                raise

            existing = self._read(operation_id)
            if (
                existing is None
                or existing["request_fingerprint"] != fingerprint
                or existing["request_json"] != item["request_json"]
            ):
                raise ProcessingOperationConflict(
                    f"Processing operation {operation_id} has different inputs"
                ) from error
            return self._response(existing)

        return self._response(item)

    def claim_work(self, operation_id: str) -> tuple[str, dict[str, Any]] | None:
        _require_digest(operation_id, "operation_id")
        worker_token = uuid4().hex

        try:
            result = self._table.update_item(
                Key={"operation_id": operation_id},
                ConditionExpression="#status = :accepted",
                UpdateExpression="SET #status = :running, worker_token = :token",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":accepted": "ACCEPTED",
                    ":running": "RUNNING",
                    ":token": worker_token,
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as error:
            if _conditional_failure(error):
                return None
            raise

        return worker_token, json.loads(result["Attributes"]["request_json"])

    def complete(
        self,
        operation_id: str,
        *,
        worker_token: str,
        result: S3ObjectReference,
    ) -> None:
        _require_digest(operation_id, "operation_id")
        if not isinstance(result, S3ObjectReference):
            raise ValueError("Expected a versioned Processing result reference")

        reference = asdict(result)
        try:
            self._table.update_item(
                Key={"operation_id": operation_id},
                ConditionExpression="#status = :running AND worker_token = :token",
                UpdateExpression="SET #status = :succeeded, #result = :result",
                ExpressionAttributeNames={
                    "#status": "status",
                    "#result": "result",
                },
                ExpressionAttributeValues={
                    ":running": "RUNNING",
                    ":succeeded": "SUCCEEDED",
                    ":token": worker_token,
                    ":result": reference,
                },
            )
        except ClientError as error:
            if not _conditional_failure(error):
                raise

            existing = self._read(operation_id)
            if (
                existing is not None
                and existing["status"] == "SUCCEEDED"
                and existing.get("worker_token") == worker_token
                and existing.get("result") == reference
            ):
                return

            raise ProcessingOperationConflict(
                f"Processing operation {operation_id} cannot record this result"
            ) from error

    def _read(self, operation_id: str) -> dict[str, Any] | None:
        return self._table.get_item(
            Key={"operation_id": operation_id},
            ConsistentRead=True,
        ).get("Item")

    @staticmethod
    def _response(item: Mapping[str, Any]) -> dict[str, Any]:
        response = {
            "response_version": 1,
            "operation_id": item["operation_id"],
            "ingestion_id": item["ingestion_id"],
            "request_fingerprint": item["request_fingerprint"],
            "status": item["status"],
        }
        if item["status"] == "SUCCEEDED":
            response["result"] = item["result"]
        return response