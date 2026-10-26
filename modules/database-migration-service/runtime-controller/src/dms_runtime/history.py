import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ingestion_registry.identity import identity_digest

from dms_runtime.json_values import to_json_value
from dms_runtime.lookup import PinnedRuntimeLookup
from dms_runtime.operations import (
    RuntimeOperationConflict,
    RuntimeOperationPending,
)


@dataclass(frozen=True)
class RecordedOperation:
    binding: dict[str, Any]
    record: dict[str, Any]


class RuntimeOperationHistory:
    def __init__(self, lookup: PinnedRuntimeLookup) -> None:
        self.lookup = lookup
        self.registry = lookup.registry

    @staticmethod
    def _identifier(name: str, value: Any) -> None:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value.encode("utf-8")) > 256
        ):
            raise ValueError(
                f"{name} must contain 1 to 256 UTF-8 bytes "
                "without surrounding whitespace"
            )

    @staticmethod
    def _fingerprint(
        *,
        ingestion_id: str,
        operation_id: str,
        action: str,
        binding: dict[str, Any],
    ) -> str:
        request_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "request_version": 1,
                    "action": action,
                    "ingestion_id": ingestion_id,
                    "operation_id": operation_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        return hashlib.sha256(
            json.dumps(
                {
                    "request_fingerprint": request_fingerprint,
                    "binding": binding,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

    def get(
        self,
        *,
        ingestion_id: str,
        operation_id: str,
    ) -> RecordedOperation:
        self._identifier("ingestion_id", ingestion_id)
        self._identifier("operation_id", operation_id)

        binding = self.lookup.resolve(ingestion_id)

        pk = f"INGESTION#{ingestion_id}"
        sk = "RUNTIME_OPERATION#" + identity_digest([operation_id])

        try:
            record = to_json_value(
                self.registry._read(pk, sk)
            )

        except (TypeError, ValueError) as error:
            raise RuntimeOperationConflict(
                "The stored runtime operation is not valid runtime JSON"
            ) from error

        if record is None:
            raise RuntimeOperationConflict(
                "The runtime operation does not exist"
            )

        if not isinstance(record, dict):
            raise RuntimeOperationConflict(
                "The stored runtime operation must be an object"
            )

        action = record.get("action")

        if action not in {"start", "resume", "stop"}:
            raise RuntimeOperationConflict(
                "The stored runtime operation has an invalid action"
            )

        expected_fingerprint = self._fingerprint(
            ingestion_id=ingestion_id,
            operation_id=operation_id,
            action=action,
            binding=binding,
        )

        if (
            record.get("pk") != pk
            or record.get("sk") != sk
            or record.get("record_type") != "RUNTIME_OPERATION"
            or record.get("ingestion_id") != ingestion_id
            or record.get("operation_id") != operation_id
            or record.get("replication_task_arn")
            != binding["resources"]["replication_task_arn"]
            or record.get("operation_fingerprint")
            != expected_fingerprint
        ):
            raise RuntimeOperationConflict(
                "The runtime operation does not match "
                "the authorised pinned binding"
            )

        return RecordedOperation(
            binding=binding,
            record=record,
        )

    def full_load_start(
        self,
        *,
        ingestion_id: str,
        operation_id: str,
    ) -> tuple[dict[str, Any], datetime]:
        stored = self.get(
            ingestion_id=ingestion_id,
            operation_id=operation_id,
        )
        record = stored.record

        if record["action"] != "start":
            raise RuntimeOperationConflict(
                "Full Load checks require an initial start operation"
            )

        if record.get("status") == "DISPATCHING":
            raise RuntimeOperationPending(
                "The start operation has no recorded outcome; "
                "reconcile it before checking Full Load"
            )

        result = record.get("result")

        if (
            record.get("status") != "RECORDED"
            or not isinstance(result, dict)
            or result.get("action") != "start"
            or result.get("outcome") != "REQUESTED"
            or result.get("replication_task_arn")
            != record["replication_task_arn"]
        ):
            raise RuntimeOperationConflict(
                "The operation does not record an accepted "
                "initial start command"
            )

        result_fingerprint = hashlib.sha256(
            json.dumps(
                result,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

        if (
            record.get("result_fingerprint")
            != result_fingerprint
        ):
            raise RuntimeOperationConflict(
                "The recorded start result fingerprint does not match"
            )

        boundary = record.get("full_load_not_before")

        if not isinstance(boundary, str):
            raise RuntimeOperationConflict(
                "The start operation has no saved Full Load boundary"
            )

        if (
            "full_load_not_before" in result
            and result["full_load_not_before"] != boundary
        ):
            raise RuntimeOperationConflict(
                "The saved Full Load boundaries do not match"
            )

        try:
            timestamp = datetime.fromisoformat(boundary)

            if timestamp.utcoffset() is None:
                raise ValueError("Naive start boundary")

        except ValueError as error:
            raise RuntimeOperationConflict(
                "The saved Full Load boundary is invalid"
            ) from error

        return stored.binding, timestamp