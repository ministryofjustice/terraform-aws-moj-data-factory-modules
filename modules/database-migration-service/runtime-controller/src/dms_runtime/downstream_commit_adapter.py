import json
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol
from uuid import uuid4

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
        """Commit or resume the same operation and return durable evidence."""

    def lookup(
        self,
        *,
        operation_id: str,
        request_fingerprint: str,
        request: Mapping[str, Any],
    ) -> ProcessingCompletion | None:
        """Read committed evidence without performing the external write."""


def _conditional_check_failed(error: ClientError) -> bool:
    return (
        error.response.get("Error", {}).get("Code")
        == "ConditionalCheckFailedException"
    )


class DownstreamCommitAdapter:
    def __init__(
        self,
        *,
        table: Any,
        target: CommitTarget,
        component: str,
        lease_seconds: int = 120,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if component == "raw_history":
            required_fields = ("commit_id", "history_location")
        elif component == "publication":
            required_fields = ("glue_database", "glue_table")
        else:
            raise ValueError("Expected raw_history or publication")

        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise ValueError("lease_seconds must be a positive integer")

        self._table = table
        self._target = target
        self._component = component
        self._required_fields = required_fields
        self._lease_seconds = lease_seconds
        self._clock = clock

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
            if not _conditional_check_failed(error):
                raise

            existing = self._read(operation_id)
            if existing is None or any(
                existing.get(field) != value for field, value in intent.items()
            ):
                raise DownstreamCommitConflict(
                    f"{self._component} operation {operation_id} has different inputs"
                ) from error

            completion = self._invoke(existing, recover=True)
            if completion is not None:
                return completion

        claim_token = self._claim(intent)
        if claim_token is None:
            return None

        try:
            completion = self._invoke(intent, recover=True)
            if completion is None:
                completion = self._invoke(intent, recover=False)
        except Exception as error:
            try:
                self._release(operation_id, claim_token)
            except ClientError as release_error:
                error.add_note(f"Could not release commit claim: {release_error}")
            raise

        self._release(operation_id, claim_token)
        return completion

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

    def _claim(self, intent: Mapping[str, Any]) -> str | None:
        now = int(self._clock())
        claim_token = uuid4().hex
        try:
            self._table.update_item(
                Key={"operation_id": intent["operation_id"]},
                UpdateExpression=(
                    "SET commit_claim_token = :token, "
                    "commit_claim_expires_at = :expires_at"
                ),
                ConditionExpression=(
                    "component = :component AND "
                    "request_fingerprint = :fingerprint AND "
                    "request_json = :request_json AND "
                    "(attribute_not_exists(commit_claim_token) OR "
                    "commit_claim_expires_at <= :now)"
                ),
                ExpressionAttributeValues={
                    ":token": claim_token,
                    ":expires_at": now + self._lease_seconds,
                    ":component": intent["component"],
                    ":fingerprint": intent["request_fingerprint"],
                    ":request_json": intent["request_json"],
                    ":now": now,
                },
            )
        except ClientError as error:
            if not _conditional_check_failed(error):
                raise
            return None
        return claim_token

    def _release(self, operation_id: str, claim_token: str) -> None:
        try:
            self._table.update_item(
                Key={"operation_id": operation_id},
                UpdateExpression="REMOVE commit_claim_token, commit_claim_expires_at",
                ConditionExpression="commit_claim_token = :token",
                ExpressionAttributeValues={":token": claim_token},
            )
        except ClientError as error:
            if not _conditional_check_failed(error):
                raise

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