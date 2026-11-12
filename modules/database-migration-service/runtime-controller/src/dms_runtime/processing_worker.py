from collections.abc import Mapping
from typing import Any, Protocol

from dms_runtime.processing_component import ProcessingComponent
from dms_runtime.processing_inputs import (
    ProcessingInputError,
    VerifiedProcessingInputs,
)
from ingestion_registry.references import S3ObjectReference


class ProcessingInputVerifier(Protocol):
    def verify(
        self,
        request: Mapping[str, Any],
    ) -> VerifiedProcessingInputs:
        """Verify the pinned manifest and contract for the request."""


class ProcessingExecutor(Protocol):
    def execute(
        self,
        *,
        operation_id: str,
        request: Mapping[str, Any],
        inputs: VerifiedProcessingInputs,
    ) -> Mapping[str, Any]:
        """Process the exact verified input references and return output evidence."""


class ProcessingResultStore(Protocol):
    def write(
        self,
        *,
        operation_id: str,
        worker_token: str,
        request: Mapping[str, Any],
        output: Mapping[str, Any],
    ) -> S3ObjectReference:
        """Write and verify durable result evidence for the claimed operation."""

    def find_verified(
        self,
        *,
        operation_id: str,
        worker_token: str,
        request: Mapping[str, Any],
    ) -> S3ObjectReference | None:
        """Find and verify evidence already written for this worker claim."""


class ProcessingWorker:
    def __init__(
        self,
        *,
        component: ProcessingComponent,
        verifier: ProcessingInputVerifier,
        executor: ProcessingExecutor,
        results: ProcessingResultStore,
    ) -> None:
        self._component = component
        self._verifier = verifier
        self._executor = executor
        self._results = results

    def run(self, operation_id: str) -> S3ObjectReference | None:
        claimed = self._component.claim_work(operation_id)
        if claimed is None:
            return None

        worker_token, request = claimed
        try:
            inputs = self._verifier.verify(request)
            output = self._executor.execute(
                operation_id=operation_id,
                request=request,
                inputs=inputs,
            )
        except ProcessingInputError:
            self._component.fail(
                operation_id,
                worker_token=worker_token,
                failure_code="INVALID_PINNED_INPUT",
            )
            raise

        result = self._results.write(
            operation_id=operation_id,
            worker_token=worker_token,
            request=request,
            output=output,
        )
        self._component.complete(
            operation_id,
            worker_token=worker_token,
            result=result,
        )
        return result

    def recover_written_result(
        self,
        operation_id: str,
    ) -> S3ObjectReference | None:
        claimed = self._component.read_running_work(operation_id)
        if claimed is None:
            return None

        worker_token, request = claimed
        result = self._results.find_verified(
            operation_id=operation_id,
            worker_token=worker_token,
            request=request,
        )
        if result is None:
            return None

        self._component.complete(
            operation_id,
            worker_token=worker_token,
            result=result,
        )
        return result