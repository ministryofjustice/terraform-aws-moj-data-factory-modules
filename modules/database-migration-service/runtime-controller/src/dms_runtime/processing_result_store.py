import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from botocore.exceptions import ClientError

from ingestion_registry.references import S3ObjectReference


MAX_RESULT_BYTES = 256 * 1024


class ProcessingEvidenceConflict(RuntimeError):
    """Stored evidence cannot be used for this Processing claim."""


def _canonical_json(document: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(document),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _error_code(error: ClientError) -> str:
    return error.response.get("Error", {}).get("Code", "")


class S3ProcessingResultStore:
    def __init__(
        self,
        client: Any,
        *,
        bucket_name: str,
        prefix: str,
        expected_bucket_owner: str,
    ) -> None:
        if not bucket_name or bucket_name != bucket_name.strip():
            raise ValueError("A result bucket is required")
        if (
            not prefix
            or prefix != prefix.strip()
            or prefix.startswith("/")
            or not prefix.endswith("/")
            or any(part in {"", ".", ".."} for part in prefix[:-1].split("/"))
        ):
            raise ValueError("Use a relative result prefix ending in '/'")
        if re.fullmatch(r"[0-9]{12}", expected_bucket_owner) is None:
            raise ValueError("expected_bucket_owner must contain 12 digits")

        self._client = client
        self._bucket_name = bucket_name
        self._prefix = prefix
        self._expected_bucket_owner = expected_bucket_owner

    def validate_size(
        self,
        *,
        operation_id: str,
        worker_token: str,
        request: Mapping[str, Any],
        output: Mapping[str, Any],
    ) -> None:
        context = self._context(operation_id, worker_token, request)
        self._encode_evidence(context, output)

    @staticmethod
    def _encode_evidence(
        context: Mapping[str, Any],
        output: Mapping[str, Any],
    ) -> bytes:
        if not isinstance(output, Mapping) or not output:
            raise ValueError("Processing output evidence must be a non-empty object")

        raw = _canonical_json({**context, "output": dict(output)})
        if len(raw) > MAX_RESULT_BYTES:
            raise ValueError("Processing evidence exceeds 256 KiB")
        return raw

    def write(
        self,
        *,
        operation_id: str,
        worker_token: str,
        request: Mapping[str, Any],
        output: Mapping[str, Any],
    ) -> S3ObjectReference:
        key = self._key(operation_id, worker_token)
        context = self._context(operation_id, worker_token, request)
        expected = self._encode_evidence(context, output)

        try:
            response = self._client.put_object(
                Bucket=self._bucket_name,
                Key=key,
                Body=expected,
                ContentType="application/json",
                ExpectedBucketOwner=self._expected_bucket_owner,
                IfNoneMatch="*",
            )
        except ClientError as error:
            if _error_code(error) not in {"PreconditionFailed", "412"}:
                raise
            stored = self._read(key)
        else:
            version_id = self._require_version(response.get("VersionId"))
            stored = self._read(key, version_id=version_id)

        if stored is None:
            raise ProcessingEvidenceConflict(
                "Processing evidence disappeared before verification"
            )

        reference, raw = stored
        if raw != expected:
            raise ProcessingEvidenceConflict(
                "Stored Processing evidence differs from the submitted result"
            )
        return reference

    def find_verified(
        self,
        *,
        operation_id: str,
        worker_token: str,
        request: Mapping[str, Any],
    ) -> S3ObjectReference | None:
        key = self._key(operation_id, worker_token)
        context = self._context(operation_id, worker_token, request)
        stored = self._read(key)
        if stored is None:
            return None

        reference, raw = stored
        try:
            document = json.loads(raw.decode("utf-8"))
            if not isinstance(document, dict):
                raise ValueError("Expected an evidence object")
            output = document.get("output")
            if not isinstance(output, dict) or not output:
                raise ValueError("Missing output evidence")
            expected = _canonical_json({**context, "output": output})
        except (UnicodeError, TypeError, ValueError, RecursionError) as error:
            raise ProcessingEvidenceConflict(
                "Stored Processing evidence is invalid"
            ) from error

        if raw != expected:
            raise ProcessingEvidenceConflict(
                "Stored Processing evidence does not match the claim and pinned request"
            )
        return reference

    def _key(self, operation_id: str, worker_token: str) -> str:
        if re.fullmatch(r"[0-9a-f]{64}", operation_id) is None:
            raise ValueError("Invalid Processing operation ID")
        if re.fullmatch(r"[0-9a-f]{32}", worker_token) is None:
            raise ValueError("Invalid Processing worker token")
        return f"{self._prefix}{operation_id}/{worker_token}/result.json"

    @staticmethod
    def _context(
        operation_id: str,
        worker_token: str,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "evidence_version": 1,
            "operation_id": operation_id,
            "worker_token": worker_token,
            "ingestion_id": request["ingestion_id"],
            "table": request["deployment"]["table"],
            "request_fingerprint": hashlib.sha256(
                _canonical_json(request)
            ).hexdigest(),
            "raw_batch": request["raw_batch"],
            "contract": request["contract"],
        }

    @staticmethod
    def _require_version(value: Any) -> str:
        if not isinstance(value, str) or not value or value == "null":
            raise ProcessingEvidenceConflict(
                "Processing evidence requires a versioned S3 object"
            )
        return value

    def _read(
        self,
        key: str,
        *,
        version_id: str | None = None,
    ) -> tuple[S3ObjectReference, bytes] | None:
        arguments = {
            "Bucket": self._bucket_name,
            "Key": key,
            "ExpectedBucketOwner": self._expected_bucket_owner,
        }
        if version_id is not None:
            arguments["VersionId"] = version_id

        try:
            response = self._client.get_object(**arguments)
        except ClientError as error:
            if version_id is None and _error_code(error) == "NoSuchKey":
                return None
            raise

        body = response["Body"]
        try:
            actual_version = self._require_version(response.get("VersionId"))
            if version_id is not None and actual_version != version_id:
                raise ProcessingEvidenceConflict(
                    "S3 returned a different Processing evidence version"
                )
            raw = body.read(MAX_RESULT_BYTES + 1)
            if len(raw) > MAX_RESULT_BYTES:
                raise ProcessingEvidenceConflict(
                    "Stored Processing evidence exceeds 256 KiB"
                )
        finally:
            body.close()

        return (
            S3ObjectReference(
                bucket_name=self._bucket_name,
                object_key=key,
                version_id=actual_version,
                sha256=hashlib.sha256(raw).hexdigest(),
            ),
            raw,
        )