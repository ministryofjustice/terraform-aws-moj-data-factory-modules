import re
from collections.abc import Callable, Mapping
from typing import Any

from dms_runtime.batch_processing import ProcessingCompletion
from ingestion_registry.references import S3ObjectReference


class ProcessingProtocolError(ValueError):
    """The component response cannot be used as Processing evidence."""


class ProcessingFailed(RuntimeError):
    """The component reported a terminal failure for this operation."""


class BatchProcessingAdapter:
    """Translate batch Processing requests and results for a bound component."""

    def __init__(
        self,
        invoke: Callable[[dict[str, Any]], Mapping[str, Any]],
    ) -> None:
        self._invoke = invoke

    def submit(
        self,
        *,
        operation_id: str,
        request_fingerprint: str,
        request: Mapping[str, Any],
    ) -> ProcessingCompletion | None:
        response = self._invoke(
            {
                "request_version": 1,
                "action": "submit_processing",
                "operation_id": operation_id,
                "request_fingerprint": request_fingerprint,
                "request": dict(request),
            }
        )
        return self._read_result(
            response,
            operation_id=operation_id,
            expected_ingestion_id=request["ingestion_id"],
            expected_fingerprint=request_fingerprint,
        )

    def lookup(
        self,
        *,
        operation_id: str,
    ) -> ProcessingCompletion | None:
        response = self._invoke(
            {
                "request_version": 1,
                "action": "lookup_processing",
                "operation_id": operation_id,
            }
        )
        return self._read_result(response, operation_id=operation_id)

    @staticmethod
    def _read_result(
        response: Mapping[str, Any],
        *,
        operation_id: str,
        expected_ingestion_id: str | None = None,
        expected_fingerprint: str | None = None,
    ) -> ProcessingCompletion | None:
        if not isinstance(response, Mapping):
            raise ProcessingProtocolError("Expected a Processing response object")

        version = response.get("response_version")
        if type(version) is not int or version != 1:
            raise ProcessingProtocolError("Unsupported Processing response version")

        if response.get("operation_id") != operation_id:
            raise ProcessingProtocolError("Processing operation ID does not match")

        status = response.get("status")
        if status == "NOT_FOUND" and expected_ingestion_id is None:
            return None

        if status not in ("ACCEPTED", "RUNNING", "SUCCEEDED", "FAILED"):
            raise ProcessingProtocolError("Unexpected Processing response status")

        ingestion_id = response.get("ingestion_id")
        if (
            not isinstance(ingestion_id, str)
            or not ingestion_id
            or ingestion_id != ingestion_id.strip()
        ):
            raise ProcessingProtocolError("Invalid Processing ingestion ID")

        fingerprint = response.get("request_fingerprint")
        if not isinstance(fingerprint, str) or not re.fullmatch(
            r"[0-9a-f]{64}", fingerprint
        ):
            raise ProcessingProtocolError("Invalid Processing request fingerprint")

        if (
            expected_ingestion_id is not None
            and ingestion_id != expected_ingestion_id
        ):
            raise ProcessingProtocolError("Processing ingestion ID does not match")

        if (
            expected_fingerprint is not None
            and fingerprint != expected_fingerprint
        ):
            raise ProcessingProtocolError("Processing request fingerprint does not match")

        if status in ("ACCEPTED", "RUNNING"):
            return None

        if status == "FAILED":
            raise ProcessingFailed(f"Processing operation {operation_id} failed")

        try:
            result = S3ObjectReference(**response["result"])
        except (KeyError, TypeError, ValueError) as error:
            raise ProcessingProtocolError(
                "Processing success requires a versioned result reference"
            ) from error

        return ProcessingCompletion(
            operation_id=operation_id,
            ingestion_id=ingestion_id,
            request_fingerprint=fingerprint,
            result_reference={
                "kind": "processing-result",
                "bucket_name": result.bucket_name,
                "object_key": result.object_key,
                "version_id": result.version_id,
                "sha256": result.sha256,
            },
        )