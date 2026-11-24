import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any

from botocore.exceptions import ClientError

from dms_runtime.processing_inputs import MAX_MANIFEST_BYTES
from ingestion_registry.ownership import OwnershipClaim
from ingestion_registry.progress import IngestionProgress, ProgressConflict
from ingestion_registry.references import S3ObjectReference


class RawBatchManifestConflict(ValueError):
    """The proposed batch conflicts with the pinned extraction evidence."""


class RawBatchManifestProducer:
    def __init__(
        self,
        *,
        client: Any,
        progress: IngestionProgress,
        bucket_name: str,
        prefix: str,
        expected_bucket_owner: str,
    ) -> None:
        if not bucket_name or bucket_name != bucket_name.strip():
            raise ValueError("A manifest bucket is required")
        if not _valid_prefix(prefix):
            raise ValueError("Use a relative manifest prefix ending in '/'")
        if re.fullmatch(r"[0-9]{12}", expected_bucket_owner) is None:
            raise ValueError("expected_bucket_owner must contain 12 digits")

        self._client = client
        self._progress = progress
        self._bucket_name = bucket_name
        self._prefix = prefix
        self._expected_bucket_owner = expected_bucket_owner

    def select_once(self, claim, *, deployment, select):
        key_id = hashlib.sha256(
            claim.ingestion_id.encode("utf-8")
        ).hexdigest()
        key = f"{self._prefix}{key_id}/selection.json"
        arguments = {
            "Bucket": self._bucket_name,
            "Key": key,
            "ExpectedBucketOwner": self._expected_bucket_owner,
        }

        def read_selection():
            listing = self._client.list_objects_v2(
                Bucket=self._bucket_name,
                Prefix=key,
                MaxKeys=1,
                ExpectedBucketOwner=self._expected_bucket_owner,
            )
            if not any(
                item.get("Key") == key
                for item in listing.get("Contents", [])
            ):
                return None
            try:
                response = self._client.get_object(**arguments)
            except ClientError as error:
                code = error.response.get("Error", {}).get("Code")
                if code in {"NoSuchKey", "404"}:
                    return None
                raise

            body = response["Body"]
            try:
                raw = body.read(MAX_MANIFEST_BYTES + 1)
            finally:
                body.close()

            if (
                not response.get("VersionId")
                or response["VersionId"] == "null"
                or len(raw) > MAX_MANIFEST_BYTES
            ):
                raise RawBatchManifestConflict(
                    "Invalid frozen Raw selection"
                )

            try:
                document = json.loads(raw)
                objects = tuple(
                    S3ObjectReference(**item)
                    for item in document["objects"]
                )
                expected = _manifest_bytes(
                    claim.ingestion_id, deployment, objects
                )
            except (KeyError, TypeError, ValueError) as error:
                raise RawBatchManifestConflict(
                    "Invalid frozen Raw selection"
                ) from error

            if raw != expected:
                raise RawBatchManifestConflict(
                    "Selection differs from pinned ingestion"
                )
            return objects

        existing = read_selection()
        if existing is not None:
            return existing

        objects = tuple(select())
        expected = _manifest_bytes(
            claim.ingestion_id, deployment, objects
        )

        try:
            self._client.put_object(
                **arguments,
                Body=expected,
                ContentType="application/json",
                IfNoneMatch="*",
            )
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code not in {"PreconditionFailed", "412"}:
                raise

        frozen = read_selection()
        if frozen is None:
            raise RawBatchManifestConflict(
                "Frozen Raw selection was not found"
            )
        return frozen

    def completed(self, claim, *, deployment):
        stage = self._progress.read_stage(
            ingestion_id=claim.ingestion_id,
            stage="EXTRACTION",
        )
        if stage is None or stage.get("status") == "RUNNING":
            return None
        if stage.get("status") != "SUCCEEDED":
            raise RawBatchManifestConflict(
                "Extraction requires explicit recovery"
            )

        evidence = stage.get("result_reference") or {}
        if evidence.get("kind") != "raw-batch":
            raise RawBatchManifestConflict(
                "Expected completed Raw batch evidence"
            )

        reference = S3ObjectReference(**{
            field: evidence[field]
            for field in (
                "bucket_name", "object_key", "version_id", "sha256"
            )
        })
        expected_prefix = (
            self._prefix
            + hashlib.sha256(
                claim.ingestion_id.encode("utf-8")
            ).hexdigest()
            + "/"
        )
        if (
            reference.bucket_name != self._bucket_name
            or not reference.object_key.startswith(expected_prefix)
        ):
            raise RawBatchManifestConflict(
                "Manifest is outside this ingestion boundary"
            )

        response = self._client.get_object(
            Bucket=reference.bucket_name,
            Key=reference.object_key,
            VersionId=reference.version_id,
            ExpectedBucketOwner=self._expected_bucket_owner,
        )
        body = response["Body"]
        try:
            raw = body.read(MAX_MANIFEST_BYTES + 1)
        finally:
            body.close()

        if (
            response.get("VersionId") != reference.version_id
            or len(raw) > MAX_MANIFEST_BYTES
            or hashlib.sha256(raw).hexdigest() != reference.sha256
        ):
            raise RawBatchManifestConflict(
                "Recorded manifest version or checksum differs"
            )

        try:
            document = json.loads(raw)
            objects = [
                S3ObjectReference(**item)
                for item in document["objects"]
            ]
            expected = _manifest_bytes(
                claim.ingestion_id, deployment, objects
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RawBatchManifestConflict(
                "Invalid recorded Raw manifest"
            ) from error

        if raw != expected:
            raise RawBatchManifestConflict(
                "Manifest differs from the pinned ingestion"
            )

        return self.commit(
            claim, deployment=deployment, objects=objects
        )

    def commit(
        self,
        claim: OwnershipClaim,
        *,
        deployment: Mapping[str, Any],
        objects: Sequence[S3ObjectReference],
    ) -> S3ObjectReference:
        ingestion_id = claim.ingestion_id
        key_id = hashlib.sha256(ingestion_id.encode("utf-8")).hexdigest()
        expected = _manifest_bytes(ingestion_id, deployment, objects)
        digest = hashlib.sha256(expected).hexdigest()
        key = f"{self._prefix}{key_id}/{digest}/manifest.json"

        stage = self._progress.read_stage(
            ingestion_id=ingestion_id,
            stage="EXTRACTION",
        )
        if stage is not None and stage["status"] == "SUCCEEDED":
            return self._verify_completed(stage, key, expected)

        if stage is not None and stage["status"] != "RUNNING":
            raise RawBatchManifestConflict(
                f"Ingestion {ingestion_id}: extraction requires recovery "
                "before committing a Raw batch"
            )

        reference = self._write_or_recover(key, expected)
        revision = int(stage["revision"]) if stage is not None else 0
        update_id = f"raw-batch-{revision}-{reference.sha256}"

        try:
            self._progress.record_stage(
                claim,
                stage="EXTRACTION",
                status="SUCCEEDED",
                update_id=update_id,
                expected_revision=revision,
                result_reference={"kind": "raw-batch", **asdict(reference)},
            )
        except ProgressConflict:
            current = self._progress.read_stage(
                ingestion_id=ingestion_id,
                stage="EXTRACTION",
            )
            if (
                current is None
                or current["status"] != "SUCCEEDED"
                or current.get("result_reference")
                != {"kind": "raw-batch", **asdict(reference)}
            ):
                raise
            return self._verify_completed(current, key, expected)

        return reference

    def _write_or_recover(self, key: str, expected: bytes) -> S3ObjectReference:
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
            code = error.response.get("Error", {}).get("Code")
            if code not in {"PreconditionFailed", "412"}:
                raise
            return self._read_exact(key, expected)

        version_id = response.get("VersionId")
        if not isinstance(version_id, str) or not version_id or version_id == "null":
            raise RawBatchManifestConflict("Raw manifest bucket must be versioned")
        return self._read_exact(key, expected, version_id=version_id)

    def _verify_completed(
        self,
        stage: Mapping[str, Any],
        key: str,
        expected: bytes,
    ) -> S3ObjectReference:
        evidence = stage.get("result_reference")
        if (
            not isinstance(evidence, dict)
            or evidence.get("kind") != "raw-batch"
            or evidence.get("bucket_name") != self._bucket_name
            or evidence.get("object_key") != key
            or evidence.get("sha256") != hashlib.sha256(expected).hexdigest()
        ):
            raise RawBatchManifestConflict(
                "The proposed batch differs from the completed extraction"
            )

        version_id = evidence.get("version_id")
        if not isinstance(version_id, str) or not version_id or version_id == "null":
            raise RawBatchManifestConflict("Recorded Raw manifest has no version")
        return self._read_exact(key, expected, version_id=version_id)

    def _read_exact(
        self,
        key: str,
        expected: bytes,
        *,
        version_id: str | None = None,
    ) -> S3ObjectReference:
        arguments = {
            "Bucket": self._bucket_name,
            "Key": key,
            "ExpectedBucketOwner": self._expected_bucket_owner,
        }
        if version_id is not None:
            arguments["VersionId"] = version_id

        response = self._client.get_object(**arguments)
        body = response["Body"]
        try:
            actual_version = response.get("VersionId")
            if (
                not isinstance(actual_version, str)
                or not actual_version
                or actual_version == "null"
                or (version_id is not None and actual_version != version_id)
            ):
                raise RawBatchManifestConflict("S3 returned an invalid manifest version")

            raw = body.read(MAX_MANIFEST_BYTES + 1)
            if raw != expected:
                raise RawBatchManifestConflict(
                    "Stored Raw manifest differs from the proposed batch"
                )
        finally:
            body.close()

        return S3ObjectReference(
            bucket_name=self._bucket_name,
            object_key=key,
            version_id=actual_version,
            sha256=hashlib.sha256(raw).hexdigest(),
        )


def _valid_prefix(prefix: Any) -> bool:
    return (
        isinstance(prefix, str)
        and prefix == prefix.strip()
        and prefix.endswith("/")
        and "\\" not in prefix
        and all(part not in {"", ".", ".."} for part in prefix[:-1].split("/"))
    )


def _manifest_bytes(
    ingestion_id: str,
    deployment: Mapping[str, Any],
    objects: Sequence[S3ObjectReference],
) -> bytes:
    landing = deployment["landing"]
    bucket = landing["bucket_name"]
    prefix = landing["prefix"]
    if not isinstance(bucket, str) or not bucket or not _valid_prefix(prefix):
        raise ValueError("Invalid pinned Raw landing boundary")

    table = {
        "schema_name": deployment["table"]["schema_name"],
        "table_name": deployment["table"]["table_name"],
    }
    generation = deployment["extraction_generation"]
    if any(
        not isinstance(value, str) or not value or value != value.strip()
        for value in (generation, *table.values())
    ):
        raise ValueError("Invalid pinned table or extraction generation")

    selected = {}
    for reference in objects:
        if not isinstance(reference, S3ObjectReference):
            raise ValueError("Expected a versioned Raw object reference")
        if (
            reference.bucket_name != bucket
            or not reference.object_key.startswith(prefix)
        ):
            raise ValueError("Raw object is outside the pinned landing boundary")
        if reference.object_key in selected:
            raise ValueError("Raw batch contains a duplicate object key")
        selected[reference.object_key] = reference

    document = {
        "manifest_version": 1,
        "ingestion_id": ingestion_id,
        "extraction_generation": generation,
        "table": table,
        "objects": [asdict(selected[key]) for key in sorted(selected)],
    }
    encoded = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ValueError("Raw manifest exceeds the configured size limit")
    return encoded