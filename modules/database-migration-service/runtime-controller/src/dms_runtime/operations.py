import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from botocore.exceptions import ClientError

from ingestion_registry.identity import identity_digest

from dms_runtime.json_values import to_json_value
from dms_runtime.ownership import RegistryScopeGuard
from dms_runtime.requests import RuntimeRequest


COMMAND_ACTIONS = frozenset({"start", "resume", "stop"})


class RuntimeOperationConflict(RuntimeError):
    """An operation conflicts with recorded content or command ordering."""


class RuntimeOperationPending(RuntimeError):
    """A previous command needs reconciliation before work can continue."""


@dataclass(frozen=True)
class OperationAdmission:
    record: dict[str, Any]
    dispatch_token: str | None

    @property
    def may_dispatch(self) -> bool:
        return self.dispatch_token is not None


@dataclass(frozen=True)
class OperationObservation:
    request: RuntimeRequest
    task_key: str
    sequence: int


class RuntimeOperations:
    def __init__(self, guard: RegistryScopeGuard) -> None:
        self.guard = guard
        self.registry = guard.registry

    @staticmethod
    def _digest(value: Any) -> str:
        return hashlib.sha256(
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

    def _timestamp(self) -> str:
        now = self.guard.clock()

        if (
            not isinstance(now, int)
            or isinstance(now, bool)
            or now < 0
        ):
            raise ValueError(
                "Registry clock must return non-negative epoch seconds"
            )

        return datetime.fromtimestamp(
            now,
            tz=timezone.utc,
        ).isoformat()

    @staticmethod
    def _key(request: RuntimeRequest) -> tuple[str, str]:
        return (
            f"INGESTION#{request.ingestion_id}",
            "RUNTIME_OPERATION#"
            + identity_digest([request.operation_id]),
        )

    def _validate(self, request: RuntimeRequest) -> None:
        if (
            not isinstance(request, RuntimeRequest)
            or request.action not in COMMAND_ACTIONS
        ):
            raise ValueError(
                "A start, resume or stop RuntimeRequest is required"
            )

        ingestion = self.guard.ingestion_claim
        task = self.guard.task_claim

        if (
            request.ingestion_id != ingestion.ingestion_id
            or request.ingestion_owner.execution_id
            != ingestion.execution_id
            or request.ingestion_owner.fencing_token
            != ingestion.fencing_token
            or request.ingestion_owner.attempt != ingestion.attempt
            or request.task_owner.execution_id != task.execution_id
            or request.task_owner.group_fencing_token
            != task.group_fencing_token
            or request.task_owner.task_fencing_token
            != task.task_fencing_token
        ):
            raise ValueError(
                "Request ownership does not match the runtime guard"
            )

    def _fingerprint(
        self,
        request: RuntimeRequest,
        binding: dict[str, Any],
    ) -> str:
        return self._digest(
            {
                "request_fingerprint": request.operation_fingerprint,
                "binding": binding,
            }
        )

    @staticmethod
    def _validate_result(
        result: Any,
        *,
        action: str,
        task_arn: str,
    ) -> None:
        if (
            not isinstance(result, dict)
            or result.get("action") != action
            or result.get("replication_task_arn") != task_arn
            or not isinstance(result.get("task_status"), str)
            or not result["task_status"]
        ):
            raise RuntimeOperationConflict(
                "The operation result does not match its action and task"
            )

        allowed_outcomes = {
            "start": {
                "REQUESTED",
                "ALREADY_RUNNING",
                "IN_PROGRESS",
            },
            "resume": {
                "REQUESTED",
                "ALREADY_RUNNING",
                "IN_PROGRESS",
            },
            "stop": {
                "REQUESTED",
                "ALREADY_STOPPED",
                "IN_PROGRESS",
                "WAITING",
                "NOT_STARTED",
            },
        }

        if result.get("outcome") not in allowed_outcomes[action]:
            raise RuntimeOperationConflict(
                "The operation result has an unsupported outcome"
            )

    def lookup(
        self,
        request: RuntimeRequest,
        binding: dict[str, Any],
    ) -> dict[str, Any] | None:
        self._validate(request)
        self.guard(binding, request.action)

        pk, sk = self._key(request)

        try:
            record = to_json_value(self.registry._read(pk, sk))

        except (TypeError, ValueError) as error:
            raise RuntimeOperationConflict(
                "The stored operation is not valid runtime JSON"
            ) from error

        if record is None:
            return None

        if (
            not isinstance(record, dict)
            or record.get("pk") != pk
            or record.get("sk") != sk
            or record.get("record_type") != "RUNTIME_OPERATION"
            or record.get("ingestion_id") != request.ingestion_id
            or record.get("operation_id") != request.operation_id
            or record.get("action") != request.action
            or record.get("replication_task_arn")
            != self.guard.task_claim.task_arn
            or record.get("scope_fingerprint")
            != self.guard.task_claim.scope_fingerprint
            or record.get("operation_fingerprint")
            != self._fingerprint(request, binding)
            or not isinstance(record.get("dispatch_token"), str)
            or not record["dispatch_token"]
        ):
            raise RuntimeOperationConflict(
                "The operation ID is already recorded "
                "with different or invalid content"
            )

        if record.get("status") == "RECORDED":
            result = record.get("result")

            self._validate_result(
                result,
                action=request.action,
                task_arn=self.guard.task_claim.task_arn,
            )

            if record.get("result_fingerprint") != self._digest(result):
                raise RuntimeOperationConflict(
                    "The recorded operation result fingerprint "
                    "does not match"
                )

        elif record.get("status") != "DISPATCHING":
            raise RuntimeOperationConflict(
                "The stored operation has an unsupported status"
            )

        return record

    def _read_sequence(self) -> int:
        task_key = self.guard.task_claim.task_key

        response = self.registry.client.transact_get_items(
            TransactItems=[
                {
                    "Get": {
                        "TableName": self.registry.table_name,
                        "Key": self.registry._encode(
                            {
                                "pk": task_key,
                                "sk": sort_key,
                            }
                        ),
                    }
                }
                for sort_key in (
                    "RUNTIME_SEQUENCE",
                    "RUNTIME_COMMAND",
                )
            ]
        )

        responses = response.get("Responses")

        if (
            not isinstance(responses, list)
            or len(responses) != 2
            or not all(
                isinstance(item, dict)
                for item in responses
            )
        ):
            raise RuntimeOperationConflict(
                "The physical task command snapshot is incomplete"
            )

        def decode(item: dict[str, Any]) -> dict[str, Any] | None:
            encoded = item.get("Item")

            if encoded is None:
                return None

            if not isinstance(encoded, dict):
                raise ValueError("Invalid DynamoDB item")

            return to_json_value(
                {
                    key: self.registry.deserializer.deserialize(value)
                    for key, value in encoded.items()
                }
            )

        try:
            record = decode(responses[0])
            gate = decode(responses[1])

        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeOperationConflict(
                "The physical task command snapshot is invalid"
            ) from error

        if gate is not None:
            raise RuntimeOperationPending(
                "The physical task has an unresolved command; "
                "reconcile it before inspecting state "
                "for another command"
            )

        if record is None:
            return 0

        sequence = record.get("sequence")

        if (
            record.get("pk") != task_key
            or record.get("sk") != "RUNTIME_SEQUENCE"
            or record.get("record_type") != "RUNTIME_COMMAND_SEQUENCE"
            or record.get("task_arn") != self.guard.task_claim.task_arn
            or type(sequence) is not int
            or sequence < 1
        ):
            raise RuntimeOperationConflict(
                "The physical task has an invalid command sequence"
            )

        return sequence

    def observe(
        self,
        request: RuntimeRequest,
        binding: dict[str, Any],
    ) -> OperationObservation:
        self._validate(request)
        self.guard(binding, request.action)

        return OperationObservation(
            request=request,
            task_key=self.guard.task_claim.task_key,
            sequence=self._read_sequence(),
        )

    def _sequence_put(
        self,
        *,
        previous: int,
        timestamp: str,
        operation_pk: str,
        operation_sk: str,
    ) -> dict[str, Any]:
        record = {
            "pk": self.guard.task_claim.task_key,
            "sk": "RUNTIME_SEQUENCE",
            "record_type": "RUNTIME_COMMAND_SEQUENCE",
            "task_arn": self.guard.task_claim.task_arn,
            "sequence": previous + 1,
            "operation_pk": operation_pk,
            "operation_sk": operation_sk,
            "updated_at": timestamp,
        }

        action = {
            "TableName": self.registry.table_name,
            "Item": self.registry._encode(record),
            "ConditionExpression": "attribute_not_exists(pk)",
        }

        if previous > 0:
            action.update(
                {
                    "ConditionExpression": "#sequence = :previous",
                    "ExpressionAttributeNames": {
                        "#sequence": "sequence",
                    },
                    "ExpressionAttributeValues": self.registry._encode(
                        {":previous": previous}
                    ),
                }
            )

        return {"Put": action}

    @staticmethod
    def _conditional_failure(error: ClientError) -> bool:
        return (
            error.response["Error"]["Code"]
            == "TransactionCanceledException"
            and any(
                reason.get("Code") == "ConditionalCheckFailed"
                for reason in error.response.get(
                    "CancellationReasons",
                    [],
                )
            )
        )

    def begin(
        self,
        request: RuntimeRequest | OperationObservation,
        binding: dict[str, Any],
    ) -> OperationAdmission:
        observation = (
            request
            if isinstance(request, OperationObservation)
            else None
        )

        if observation is not None:
            request = observation.request

            if (
                observation.task_key != self.guard.task_claim.task_key
                or type(observation.sequence) is not int
                or observation.sequence < 0
            ):
                raise RuntimeOperationConflict(
                    "The observation does not match the physical task"
                )

        existing = self.lookup(request, binding)

        if existing is not None:
            return OperationAdmission(existing, None)

        previous_sequence = (
            observation.sequence
            if observation is not None
            else self._read_sequence()
        )

        timestamp = self._timestamp()
        token = str(uuid4())
        pk, sk = self._key(request)

        record = {
            "pk": pk,
            "sk": sk,
            "record_type": "RUNTIME_OPERATION",
            "operation_id": request.operation_id,
            "operation_fingerprint": self._fingerprint(request, binding),
            "ingestion_id": request.ingestion_id,
            "action": request.action,
            "replication_task_arn": self.guard.task_claim.task_arn,
            "scope_fingerprint": self.guard.task_claim.scope_fingerprint,
            "dispatch_token": token,
            "command_sequence": previous_sequence + 1,
            "status": "DISPATCHING",
            "created_at": timestamp,
        }

        if request.action == "start":
            record["full_load_not_before"] = timestamp

        gate = {
            "pk": self.guard.task_claim.task_key,
            "sk": "RUNTIME_COMMAND",
            "record_type": "RUNTIME_COMMAND",
            "operation_pk": pk,
            "operation_sk": sk,
            "dispatch_token": token,
            "created_at": timestamp,
        }

        try:
            self.registry.client.transact_write_items(
                TransactItems=[
                    *self.guard.transaction_checks(
                        binding,
                        request.action,
                    ),
                    self.registry._put_new(record),
                    self.registry._put_new(gate),
                    self._sequence_put(
                        previous=previous_sequence,
                        timestamp=timestamp,
                        operation_pk=pk,
                        operation_sk=sk,
                    ),
                ],
                ClientRequestToken=token,
            )

        except ClientError as error:
            if not self._conditional_failure(error):
                raise

            existing = self.lookup(request, binding)

            if existing is not None:
                return OperationAdmission(existing, None)

            pending = self.registry._read(
                self.guard.task_claim.task_key,
                "RUNTIME_COMMAND",
            )

            if pending is not None:
                raise RuntimeOperationPending(
                    "The physical task has an unresolved command; "
                    "reconcile it before issuing another operation"
                ) from error

            if self._read_sequence() != previous_sequence:
                raise RuntimeOperationConflict(
                    "The physical task command sequence changed "
                    "after observation; inspect its state again "
                    "before reserving an operation"
                ) from error

            raise

        return OperationAdmission(record, token)

    def finish(
        self,
        request: RuntimeRequest,
        binding: dict[str, Any],
        *,
        dispatch_token: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        self._validate(request)

        if not isinstance(dispatch_token, str) or not dispatch_token:
            raise ValueError("A dispatch token is required")

        if not isinstance(result, dict):
            raise ValueError("The operation result must be an object")

        saved_result = to_json_value(result)

        encoded = json.dumps(
            saved_result,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")

        if len(encoded) > 65536:
            raise ValueError(
                "The operation result must not exceed 64 KiB"
            )

        self._validate_result(
            saved_result,
            action=request.action,
            task_arn=self.guard.task_claim.task_arn,
        )

        result_fingerprint = self._digest(saved_result)
        existing = self.lookup(request, binding)

        if existing is None:
            raise RuntimeOperationConflict(
                "The operation has not been reserved"
            )

        if existing["dispatch_token"] != dispatch_token:
            raise RuntimeOperationConflict(
                "The dispatch token does not match the operation"
            )

        if existing["status"] == "RECORDED":
            if existing["result_fingerprint"] != result_fingerprint:
                raise RuntimeOperationConflict(
                    "The operation already has a different result"
                )

            return existing

        timestamp = self._timestamp()
        pk, sk = self._key(request)
        fingerprint = self._fingerprint(request, binding)

        try:
            self.registry.client.transact_write_items(
                TransactItems=[
                    *self.guard.transaction_checks(
                        binding,
                        request.action,
                    ),
                    {
                        "Update": {
                            "TableName": self.registry.table_name,
                            "Key": self.registry._encode(
                                {"pk": pk, "sk": sk}
                            ),
                            "UpdateExpression": (
                                "SET #status = :recorded, "
                                "#result = :result, "
                                "result_fingerprint = :result_digest, "
                                "recorded_at = :now"
                            ),
                            "ConditionExpression": (
                                "#status = :dispatching "
                                "AND dispatch_token = :token "
                                "AND operation_fingerprint = :fingerprint"
                            ),
                            "ExpressionAttributeNames": {
                                "#status": "status",
                                "#result": "result",
                            },
                            "ExpressionAttributeValues": (
                                self.registry._encode(
                                    {
                                        ":recorded": "RECORDED",
                                        ":dispatching": "DISPATCHING",
                                        ":result": saved_result,
                                        ":result_digest": result_fingerprint,
                                        ":now": timestamp,
                                        ":token": dispatch_token,
                                        ":fingerprint": fingerprint,
                                    }
                                )
                            ),
                        }
                    },
                    {
                        "Delete": {
                            "TableName": self.registry.table_name,
                            "Key": self.registry._encode(
                                {
                                    "pk": self.guard.task_claim.task_key,
                                    "sk": "RUNTIME_COMMAND",
                                }
                            ),
                            "ConditionExpression": (
                                "operation_pk = :pk "
                                "AND operation_sk = :sk "
                                "AND dispatch_token = :token"
                            ),
                            "ExpressionAttributeValues": (
                                self.registry._encode(
                                    {
                                        ":pk": pk,
                                        ":sk": sk,
                                        ":token": dispatch_token,
                                    }
                                )
                            ),
                        }
                    },
                ],
                ClientRequestToken=str(uuid4()),
            )

        except ClientError as error:
            if not self._conditional_failure(error):
                raise

            recorded = self.lookup(request, binding)

            if (
                recorded is not None
                and recorded["status"] == "RECORDED"
                and recorded["dispatch_token"] == dispatch_token
                and recorded["result_fingerprint"] == result_fingerprint
            ):
                return recorded

            raise RuntimeOperationConflict(
                "The operation changed while saving its result"
            ) from error

        return {
            **existing,
            "status": "RECORDED",
            "result": saved_result,
            "result_fingerprint": result_fingerprint,
            "recorded_at": timestamp,
        }