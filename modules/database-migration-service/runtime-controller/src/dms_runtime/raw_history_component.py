import hashlib
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from botocore.exceptions import ClientError

from dms_runtime.batch_processing import ProcessingCompletion
from dms_runtime.downstream_commit_adapter import DownstreamCommitConflict
from dms_runtime.processing_executor import BoundedS3ProcessingExecutor
from dms_runtime.processing_inputs import S3ProcessingInputVerifier
from dms_runtime.processing_result_store import S3ProcessingResultStore
from ingestion_registry.references import S3ObjectReference


_MAX_VERSION_ID_PLACEHOLDER = "\x00" * 1024


class RawHistoryTarget:
    def __init__(
        self,
        *,
        client: Any,
        verifier: S3ProcessingInputVerifier,
        raw_bucket_owner: str,
        history_bucket: str,
        history_bucket_owner: str,
        history_prefix: str,
        max_batch_bytes: int,
    ) -> None:
        self._client = client
        self._verifier = verifier
        self._bucket = history_bucket
        self._owner = history_bucket_owner
        self._prefix = history_prefix
        self._commits = S3ProcessingResultStore(
            client,
            bucket_name=history_bucket,
            prefix=f"{history_prefix}commits/",
            expected_bucket_owner=history_bucket_owner,
        )
        self._executor = BoundedS3ProcessingExecutor(
            client,
            expected_bucket_owner=raw_bucket_owner,
            max_batch_bytes=max_batch_bytes,
            process=self._archive,
        )

    def commit(
        self,
        *,
        operation_id: str,
        request_fingerprint: str,
        request: Mapping[str, Any],
    ) -> ProcessingCompletion:
        inputs = self._verifier.verify(request)
        worker_token = request_fingerprint[:32]
        prefix = self._data_prefix(operation_id)
        landing_prefix = request["deployment"]["landing"]["prefix"]

        projected_objects = [
            {
                "source": asdict(source),
                "archived": {
                    "bucket_name": self._bucket,
                    "object_key": (
                        f"{prefix}{source.object_key.removeprefix(landing_prefix)}"
                    ),
                    "version_id": _MAX_VERSION_ID_PLACEHOLDER,
                    "sha256": source.sha256,
                },
            }
            for source in inputs.raw_objects
        ]
        self._commits.validate_size(
            operation_id=operation_id,
            worker_token=worker_token,
            request=request,
            output={
                "history_location": f"s3://{self._bucket}/{prefix}",
                "contract": asdict(inputs.contract),
                "objects": projected_objects,
            },
        )

        output = self._executor.execute(
            operation_id=operation_id,
            request=request,
            inputs=inputs,
        )
        reference = self._commits.write(
            operation_id=operation_id,
            worker_token=worker_token,
            request=request,
            output=output,
        )
        return self._completion(
            operation_id, request_fingerprint, request, reference
        )

    def lookup(
        self,
        *,
        operation_id: str,
        request_fingerprint: str,
        request: Mapping[str, Any],
    ) -> ProcessingCompletion | None:
        reference = self._commits.find_verified(
            operation_id=operation_id,
            worker_token=request_fingerprint[:32],
            request=request,
        )
        if reference is None:
            return None

        return self._completion(
            operation_id, request_fingerprint, request, reference
        )

    def _data_prefix(self, operation_id: str) -> str:
        if len(operation_id) != 64 or any(
            character not in "0123456789abcdef" for character in operation_id
        ):
            raise ValueError("Expected a lowercase SHA-256 operation ID")
        return f"{self._prefix}data/{operation_id}/"

    def _archive(
        self,
        *,
        operation_id: str,
        request: Mapping[str, Any],
        contract: S3ObjectReference,
        raw_objects: tuple[tuple[S3ObjectReference, bytes], ...],
    ) -> Mapping[str, Any]:
        prefix = self._data_prefix(operation_id)
        landing_prefix = request["deployment"]["landing"]["prefix"]
        archived = []

        for source, raw in raw_objects:
            relative_key = source.object_key.removeprefix(landing_prefix)
            destination = self._write_object(f"{prefix}{relative_key}", raw)
            archived.append(
                {"source": asdict(source), "archived": asdict(destination)}
            )

        return {
            "history_location": f"s3://{self._bucket}/{prefix}",
            "contract": asdict(contract),
            "objects": archived,
        }

    def _write_object(self, key: str, raw: bytes) -> S3ObjectReference:
        arguments = {
            "Bucket": self._bucket,
            "Key": key,
            "ExpectedBucketOwner": self._owner,
        }
        try:
            written = self._client.put_object(
                **arguments,
                Body=raw,
                IfNoneMatch="*",
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") not in {
                "PreconditionFailed",
                "412",
            }:
                raise
            requested_version = None
        else:
            requested_version = written.get("VersionId")
            if not requested_version or requested_version == "null":
                raise DownstreamCommitConflict("Raw History bucket must be versioned")
            arguments["VersionId"] = requested_version

        response = self._client.get_object(**arguments)
        body = response["Body"]
        try:
            actual_version = response.get("VersionId")
            if (
                not actual_version
                or actual_version == "null"
                or (
                    requested_version is not None
                    and requested_version != actual_version
                )
            ):
                raise DownstreamCommitConflict("Invalid Raw History object version")
            if body.read(len(raw) + 1) != raw:
                raise DownstreamCommitConflict(
                    "Existing Raw History object differs from the pinned input"
                )
        finally:
            body.close()

        return S3ObjectReference(
            bucket_name=self._bucket,
            object_key=key,
            version_id=actual_version,
            sha256=hashlib.sha256(raw).hexdigest(),
        )

    def _completion(
        self,
        operation_id: str,
        request_fingerprint: str,
        request: Mapping[str, Any],
        reference: S3ObjectReference,
    ) -> ProcessingCompletion:
        return ProcessingCompletion(
            operation_id=operation_id,
            ingestion_id=request["ingestion_id"],
            request_fingerprint=request_fingerprint,
            result_reference={
                "kind": "raw-history-commit",
                "commit_id": reference.sha256,
                "history_location": (
                    f"s3://{self._bucket}/{self._data_prefix(operation_id)}"
                ),
                **asdict(reference),
            },
        )