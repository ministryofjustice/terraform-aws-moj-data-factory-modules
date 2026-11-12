import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ingestion_registry.references import S3ObjectReference
from botocore.exceptions import (
    ClientError,
    ConnectionError as BotocoreConnectionError,
    HTTPClientError,
    IncompleteReadError,
)


MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_CONTRACT_BYTES = 1024 * 1024


class ProcessingInputError(ValueError):
    """Pinned inputs cannot be used for this Processing operation."""


@dataclass(frozen=True)
class VerifiedProcessingInputs:
    raw_manifest: S3ObjectReference
    contract: S3ObjectReference
    raw_objects: tuple[S3ObjectReference, ...]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")

class RetryableProcessingReadError(RuntimeError):
    """A pinned S3 input could not be read before processing started."""


def read_pinned_input(
    client: Any,
    reference: S3ObjectReference,
    *,
    owner: str,
    limit: int,
    label: str,
    size_error: str,
) -> bytes:
    try:
        response = client.get_object(
            Bucket=reference.bucket_name,
            Key=reference.object_key,
            VersionId=reference.version_id,
            ExpectedBucketOwner=owner,
        )
        body = response["Body"]
        try:
            if response.get("VersionId") != reference.version_id:
                raise ProcessingInputError(f"{label} object version does not match")
            raw = body.read(limit + 1)
        finally:
            body.close()
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code")
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in {"NoSuchKey", "NoSuchVersion"}:
            raise ProcessingInputError(f"{label} pinned version is missing") from error
        if (
            code in {"SlowDown", "RequestTimeout", "Throttling", "ThrottlingException"}
            or status in {429, 500, 502, 503, 504}
        ):
            raise RetryableProcessingReadError(f"{label} read failed") from error
        raise
    except (
        BotocoreConnectionError,
        HTTPClientError,
        IncompleteReadError,
        TimeoutError,
    ) as error:
        raise RetryableProcessingReadError(f"{label} read failed") from error

    if len(raw) > limit:
        raise ProcessingInputError(size_error)
    if hashlib.sha256(raw).hexdigest() != reference.sha256:
        raise ProcessingInputError(f"{label} checksum does not match")
    return raw


class S3ProcessingInputVerifier:
    """Verify a pinned Raw batch manifest and registered contract."""

    def __init__(
        self,
        client: Any,
        *,
        raw_bucket_owner: str,
        contract_bucket_owner: str,
    ) -> None:
        for owner in (raw_bucket_owner, contract_bucket_owner):
            if not isinstance(owner, str) or not re.fullmatch(r"[0-9]{12}", owner):
                raise ValueError("Bucket owners must be 12-digit account IDs")

        self._client = client
        self._raw_bucket_owner = raw_bucket_owner
        self._contract_bucket_owner = contract_bucket_owner

    def verify(self, request: Mapping[str, Any]) -> VerifiedProcessingInputs:
        try:
            deployment = request["deployment"]
            table = {
                "schema_name": deployment["table"]["schema_name"],
                "table_name": deployment["table"]["table_name"],
            }
            landing = deployment["landing"]
            bucket = landing["bucket_name"]
            prefix = landing["prefix"]
            manifest_ref = S3ObjectReference(**request["raw_batch"])

            contract_fields = request["contract"]
            if contract_fields["kind"] != "registered-contract":
                raise ValueError("Expected a registered contract")
            contract_ref = S3ObjectReference(
                bucket_name=contract_fields["contract_bucket_name"],
                object_key=contract_fields["contract_object_key"],
                version_id=contract_fields["contract_object_version_id"],
                sha256=contract_fields["contract_object_sha256"],
            )
            manifest_identity = {
                "ingestion_id": request["ingestion_id"],
                "extraction_generation": deployment["extraction_generation"],
                "table": table,
            }
            contract_identity = {
                "source": deployment["source"],
                "database_name": deployment["database_name"],
                "contract_version": contract_fields["contract_version"],
                **table,
            }
        except (KeyError, TypeError, ValueError) as error:
            raise ProcessingInputError(
                "Incomplete pinned Processing inputs"
            ) from error

        if (
            not isinstance(bucket, str)
            or not bucket
            or not isinstance(prefix, str)
            or not prefix.endswith("/")
            or any(part in {"", ".", ".."} for part in prefix[:-1].split("/"))
        ):
            raise ProcessingInputError("Invalid pinned landing boundary")

        manifest = self._read_json(
            manifest_ref,
            owner=self._raw_bucket_owner,
            limit=MAX_MANIFEST_BYTES,
            label="Raw manifest",
        )
        if (
            set(manifest) != {"manifest_version", "objects", *manifest_identity}
            or type(manifest["manifest_version"]) is not int
            or manifest["manifest_version"] != 1
        ):
            raise ProcessingInputError("Invalid v1 Raw manifest")
        if any(manifest[field] != value for field, value in manifest_identity.items()):
            raise ProcessingInputError(
                "Raw manifest does not match the pinned ingestion"
            )

        entries = manifest["objects"]
        if not isinstance(entries, list):
            raise ProcessingInputError("Raw manifest objects must be a list")

        raw_objects = []
        seen_keys = set()
        for entry in entries:
            try:
                reference = S3ObjectReference(**entry)
            except (TypeError, ValueError) as error:
                raise ProcessingInputError("Invalid Raw object reference") from error

            if (
                reference.bucket_name != bucket
                or not reference.object_key.startswith(prefix)
            ):
                raise ProcessingInputError(
                    "Raw object is outside the pinned landing boundary"
                )
            if reference == manifest_ref:
                raise ProcessingInputError("Raw manifest cannot include itself")
            if reference.object_key in seen_keys:
                raise ProcessingInputError(
                    "Raw manifest contains a duplicate object key"
                )

            seen_keys.add(reference.object_key)
            raw_objects.append(reference)

        contract = self._read_json(
            contract_ref,
            owner=self._contract_bucket_owner,
            limit=MAX_CONTRACT_BYTES,
            label="Registered contract",
        )
        if any(
            contract.get(field) != value
            for field, value in contract_identity.items()
        ):
            raise ProcessingInputError(
                "Registered contract does not match the pinned table"
            )

        return VerifiedProcessingInputs(
            raw_manifest=manifest_ref,
            contract=contract_ref,
            raw_objects=tuple(raw_objects),
        )

    def _read_json(
        self,
        reference: S3ObjectReference,
        *,
        owner: str,
        limit: int,
        label: str,
    ) -> dict[str, Any]:
        raw = read_pinned_input(
            self._client,
            reference,
            owner=owner,
            limit=limit,
            label=label,
            size_error=f"{label} exceeds its size limit",
        )

        try:
            document = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeError, ValueError, RecursionError) as error:
            raise ProcessingInputError(f"{label} is invalid JSON") from error

        if not isinstance(document, dict):
            raise ProcessingInputError(f"{label} must be a JSON object")
        return document