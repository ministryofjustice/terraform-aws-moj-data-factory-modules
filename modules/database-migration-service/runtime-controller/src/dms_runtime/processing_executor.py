import re
from collections.abc import Callable, Mapping
from typing import Any

from dms_runtime.processing_inputs import (
    VerifiedProcessingInputs,
    read_pinned_input,
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
            raw = read_pinned_input(
                self._client,
                reference,
                owner=self._owner,
                limit=remaining,
                label="Raw object",
                size_error="Raw batch exceeds the executor byte limit",
            )
            remaining -= len(raw)
            verified.append((reference, raw))

        return self._process(
            operation_id=operation_id,
            request=request,
            contract=inputs.contract,
            raw_objects=tuple(verified),
        )