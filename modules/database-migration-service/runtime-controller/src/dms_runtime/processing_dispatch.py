import re
from collections.abc import Callable, Mapping
from typing import Any

from dms_runtime.processing_worker import ProcessingWorker


_OPERATION_ID = re.compile(r"[0-9a-f]{64}")


class ProcessingDispatchPending(RuntimeError):
    """The claimed operation still has no verified completion evidence."""


def build_processing_stream_handler(
    *,
    worker: ProcessingWorker,
    lookup: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    stream_arn: str,
) -> Callable[[dict[str, Any], Any], dict[str, list[dict[str, str]]]]:
    """Dispatch operation inserts and report the first failed stream record."""
    if not isinstance(stream_arn, str) or not stream_arn.strip():
        raise ValueError("A trusted Processing operation stream ARN is required")

    def handler(
        event: dict[str, Any],
        _context: Any,
    ) -> dict[str, list[dict[str, str]]]:
        for record in event["Records"]:
            sequence = record["dynamodb"]["SequenceNumber"]

            try:
                if record.get("eventSourceARN") != stream_arn:
                    raise ValueError("Unexpected Processing operation stream")

                if record["eventName"] != "INSERT":
                    continue

                operation_id = record["dynamodb"]["Keys"]["operation_id"]["S"]
                if _OPERATION_ID.fullmatch(operation_id) is None:
                    raise ValueError("Invalid Processing operation ID")

                _dispatch(worker, lookup, operation_id)
            except Exception:
                return {
                    "batchItemFailures": [{"itemIdentifier": sequence}],
                }

        return {"batchItemFailures": []}

    return handler


def _dispatch(
    worker: ProcessingWorker,
    lookup: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    operation_id: str,
) -> None:
    if worker.run(operation_id) is not None:
        return

    if worker.recover_written_result(operation_id) is not None:
        return

    result = lookup(
        {
            "request_version": 1,
            "action": "lookup_processing",
            "operation_id": operation_id,
        }
    )
    if result["status"] in {"SUCCEEDED", "FAILED"}:
        return

    raise ProcessingDispatchPending(
        f"Processing operation {operation_id} has no terminal result"
    )