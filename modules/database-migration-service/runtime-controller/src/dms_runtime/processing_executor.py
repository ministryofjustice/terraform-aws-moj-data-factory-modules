import hashlib
import re
from collections.abc import Callable, Mapping
from typing import Any

from dms_runtime.processing_inputs import (
    ProcessingInputError,
    VerifiedProcessingInputs,
)
from ingestion_registry.references import S3ObjectReference


class BoundedS3ProcessingExecutor:
    def __init__(
        self,
        client: Any,
        *,
        expected_bucket_owner: str,
        max_batch_bytes: int,
        process: Callable[..., Mapping[str, Any]],
    ) -> None:
        if not re.fullmatch(r"[0-9]{12}", expected_bucket_owner):
            raise ValueError("Expected a 12-digit bucket owner")
        if type(max_batch_bytes) is not int or max_batch_bytes < 1:
            raise ValueError("max_batch_bytes must be a positive integer")

        self._client = client
        self._owner = expected_bucket_owner
        self._limit = max_batch_bytes
        self._process = process

    def execute(
        self,
        *,
        operation_id: str,
        request: Mapping[str, Any],
        inputs: VerifiedProcessingInputs,
    ) -> Mapping[str, Any]:
        remaining = self._limit
        verified: list[tuple[S3ObjectReference, bytes]] = []

        for reference in inputs.raw_objects:
            response = self._client.get_object(
                Bucket=reference.bucket_name,
                Key=reference.object_key,
                VersionId=reference.version_id,
                ExpectedBucketOwner=self._owner,
            )
            body = response["Body"]
            try:
                if response.get("VersionId") != reference.version_id:
                    raise ProcessingInputError("Raw object version does not match")

                raw = body.read(remaining + 1)
                if len(raw) > remaining:
                    raise ProcessingInputError(
                        "Raw batch exceeds the executor byte limit"
                    )
                if hashlib.sha256(raw).hexdigest() != reference.sha256:
                    raise ProcessingInputError("Raw object checksum does not match")
            finally:
                body.close()

            remaining -= len(raw)
            verified.append((reference, raw))

        return self._process(
            operation_id=operation_id,
            request=request,
            contract=inputs.contract,
            raw_objects=tuple(verified),
        )