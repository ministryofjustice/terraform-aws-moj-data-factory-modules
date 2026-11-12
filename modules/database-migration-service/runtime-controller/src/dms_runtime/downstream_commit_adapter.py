import json
from collections.abc import Mapping
from typing import Any, Protocol

from botocore.exceptions import ClientError

from dms_runtime.batch_processing import ProcessingCompletion


class DownstreamCommitConflict(RuntimeError):
    """The operation or committed result conflicts with the pinned request."""


class CommitTarget(Protocol):
    def commit(
        self,
        *,
        operation_id: str,
        request_fingerprint: str,
        request: Mapping[str, Any],
    ) -> ProcessingCompletion | None:
        """Commit work and return durable evidence or None while pending."""

    def lookup(
        self,
        *,
        operation_id: str,
        request_fingerprint: str,
        request: Mapping[str, Any],
    ) -> ProcessingCompletion | None:
        """Read committed evidence without performing the external write."""


class DownstreamCommitAdapter:
    def __init__(
        self,
        *,
        table: Any,
        target: CommitTarget,
        component: str,
    ) -> None:
        if component == "raw_history":
            required_fields = ("commit_id", "history_location")
        elif component == "publication":
            required_fields = ("glue_database", "glue_table")
        else:
            raise ValueError("Expected raw_history or publication")

        self._table = table
        self._target = target
        self._component = component
        self._required_fields = required_fields

    def submit(
        self,
        *,
        operation_id: str,
        request_fingerprint: str,
        request: Mapping[str, Any],
    ) -> ProcessingCompletion | None:
        request_json = json.dumps(
            dict(request),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        if len(request_json.encode("utf-8")) > 128 * 1024:
            raise ValueError("Downstream request exceeds 128 KiB")

        intent = {
            "operation_id": operation_id,
            "component": self._component,
            "request_fingerprint": request_fingerprint,
            "request_json": request_json,
        }
        try:
            self._table.put_item(
                Item=intent,
                ConditionExpression="attribute_not_exists(operation_id)",
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != (
                "ConditionalCheckFailedException"
            ):
                raise

            existing = self._read(operation_id)
            if existing is None or any(
                existing.get(field) != value for field, value in intent.items()
            ):
                raise DownstreamCommitConflict(
                    f"{self._component} operation {operation_id} has different inputs"
                ) from error

            return self._invoke(existing, recover=True)

        return self._invoke(intent, recover=False)

    def lookup(self, *, operation_id: str) -> ProcessingCompletion | None:
        intent = self._read(operation_id)
        if intent is None:
            return None
        if intent["component"] != self._component:
            raise DownstreamCommitConflict(
                f"Operation {operation_id} belongs to a different component"
            )
        return self._invoke(intent, recover=True)

    def _read(self, operation_id: str) -> dict[str, Any] | None:
        return self._table.get_item(
            Key={"operation_id": operation_id},
            ConsistentRead=True,
        ).get("Item")

    def _invoke(
        self,
        intent: Mapping[str, Any],
        *,
        recover: bool,
    ) -> ProcessingCompletion | None:
        request = json.loads(intent["request_json"])
        operation_id = intent["operation_id"]
        fingerprint = intent["request_fingerprint"]
        invoke = self._target.lookup if recover else self._target.commit
        completion = invoke(
            operation_id=operation_id,
            request_fingerprint=fingerprint,
            request=request,
        )
        if completion is None:
            return None

        if (
            completion.operation_id != operation_id
            or completion.ingestion_id != request["ingestion_id"]
            or completion.request_fingerprint != fingerprint
        ):
            raise DownstreamCommitConflict(
                f"{self._component} evidence does not match operation {operation_id}"
            )

        for field in self._required_fields:
            value = completion.result_reference.get(field)
            if not isinstance(value, str) or not value.strip():
                raise DownstreamCommitConflict(
                    f"{self._component} evidence requires {field}"
                )

        return completion