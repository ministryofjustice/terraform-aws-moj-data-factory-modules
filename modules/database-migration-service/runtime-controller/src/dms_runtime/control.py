from collections.abc import Callable
from typing import Any

from botocore.exceptions import ClientError

from dms_runtime.inspection import (
    InspectionError,
    TaskInspection,
)
from dms_runtime.operations import (
    OperationAdmission,
    OperationObservation,
    RuntimeOperationPending,
    RuntimeOperations,
)
from dms_runtime.preflight import SourcePreflight
from dms_runtime.requests import RuntimeRequest


class TaskOperationBlocked(RuntimeError):
    """The requested task operation is unsafe in its current state."""


class TaskControl:
    def __init__(
        self,
        dms_client: Any,
        inspection: TaskInspection,
        preflight: SourcePreflight,
        *,
        check_scope: Callable[[dict[str, Any], str], None],
        credential_sync_function_arn: str | None = None,
        operations: RuntimeOperations | None = None,
    ) -> None:
        if not callable(check_scope):
            raise ValueError(
                "A task-scope ownership check is required"
            )

        if operations is not None:
            if not isinstance(operations, RuntimeOperations):
                raise ValueError(
                    "operations must be RuntimeOperations"
                )

            if check_scope is not operations.guard:
                raise ValueError(
                    "Task control and operation persistence "
                    "must use the same ownership guard"
                )

            retries = dms_client.meta.config.retries

            if (
                not isinstance(retries, dict)
                or retries.get("total_max_attempts") != 1
            ):
                raise ValueError(
                    "Persisted task control requires a DMS client "
                    "configured with total_max_attempts=1"
                )

        self.dms = dms_client
        self.inspection = inspection
        self.preflight = preflight
        self.check_scope = check_scope
        self.credential_sync_function_arn = (
            credential_sync_function_arn
        )
        self.operations = operations

    def _verify(
        self,
        binding: dict[str, Any],
        action: str,
    ) -> None:
        if self.check_scope(binding, action) is not None:
            raise ValueError(
                "check_scope must raise on failure "
                "and return None on success"
            )

    @staticmethod
    def _saved_result(
        admission: OperationAdmission,
    ) -> dict[str, Any]:
        record = admission.record

        if record["status"] != "RECORDED":
            raise RuntimeOperationPending(
                "This operation may already have dispatched a command; "
                "reconcile its outcome before continuing"
            )

        return dict(record["result"])

    def _prepare(
        self,
        binding: dict[str, Any],
        action: str,
        request: RuntimeRequest | None,
    ) -> tuple[
        dict[str, Any] | None,
        OperationObservation | None,
    ]:
        if self.operations is None:
            if request is not None:
                raise ValueError(
                    "Runtime requests require operation persistence"
                )

            return None, None

        if (
            not isinstance(request, RuntimeRequest)
            or request.action != action
        ):
            raise ValueError(
                "A matching RuntimeRequest is required "
                "for persisted task control"
            )

        existing = self.operations.lookup(request, binding)

        if existing is not None:
            return (
                self._saved_result(
                    OperationAdmission(existing, None)
                ),
                None,
            )

        observation = self.operations.observe(request, binding)

        return None, observation

    def _reserve(
        self,
        binding: dict[str, Any],
        observation: OperationObservation | None,
    ) -> OperationAdmission | None:
        if self.operations is None:
            return None

        if observation is None:
            raise ValueError(
                "A task observation is required before reservation"
            )

        return self.operations.begin(observation, binding)

    def _finish(
        self,
        binding: dict[str, Any],
        request: RuntimeRequest | None,
        admission: OperationAdmission | None,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        if self.operations is None:
            return result

        if admission is None or not admission.may_dispatch:
            raise RuntimeOperationPending(
                "This invocation does not hold dispatch permission"
            )

        saved = {
            **result,
            "operation_id": request.operation_id,
        }

        if (
            request.action == "start"
            and result["outcome"] == "REQUESTED"
        ):
            saved["full_load_not_before"] = admission.record[
                "full_load_not_before"
            ]

        recorded = self.operations.finish(
            request,
            binding,
            dispatch_token=admission.dispatch_token,
            result=saved,
        )

        return dict(recorded["result"])

    def _record_observation(
        self,
        binding: dict[str, Any],
        request: RuntimeRequest | None,
        observation: OperationObservation | None,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        admission = self._reserve(binding, observation)

        if admission is not None and not admission.may_dispatch:
            return self._saved_result(admission)

        return self._finish(
            binding,
            request,
            admission,
            result,
        )

    @staticmethod
    def _observed(
        action: str,
        task: dict[str, Any],
    ) -> dict[str, Any] | None:
        status = task["status"]

        if action in {"start", "resume"}:
            outcome = {
                "running": "ALREADY_RUNNING",
                "starting": "IN_PROGRESS",
            }.get(status)

        else:
            outcome = {
                "stopped": "ALREADY_STOPPED",
                "stopping": "IN_PROGRESS",
                "starting": "WAITING",
                "ready": "NOT_STARTED",
            }.get(status)

            if status == "ready" and task.get("has_started"):
                raise TaskOperationBlocked(
                    "A previously executed task is unexpectedly ready"
                )

        if outcome is None:
            return None

        return {
            "action": action,
            "replication_task_arn": task["replication_task_arn"],
            "task_status": status,
            "outcome": outcome,
        }

    def start(
        self,
        binding: dict[str, Any],
        *,
        request: RuntimeRequest | None = None,
    ) -> dict[str, Any]:
        return self._start_or_resume(binding, "start", request)

    def resume(
        self,
        binding: dict[str, Any],
        *,
        request: RuntimeRequest | None = None,
    ) -> dict[str, Any]:
        return self._start_or_resume(binding, "resume", request)

    def _start_or_resume(
        self,
        binding: dict[str, Any],
        action: str,
        request: RuntimeRequest | None,
    ) -> dict[str, Any]:
        previous, observation = self._prepare(
            binding,
            action,
            request,
        )

        if previous is not None:
            return previous

        self._verify(binding, action)
        task = self.inspection.task_status(binding)

        if (
            action == "resume"
            and task["migration_type"] == "full-load"
        ):
            raise TaskOperationBlocked(
                "Full Load-only resume may reload partial tables; "
                "this requires a separately supported recovery strategy"
            )

        if action == "start" and task["migration_type"] == "cdc":
            raise TaskOperationBlocked(
                "Initial CDC-only start requires "
                "an explicit source-position strategy"
            )

        observed = self._observed(action, task)

        if observed is not None:
            return self._record_observation(
                binding,
                request,
                observation,
                observed,
            )

        self._require_start_state(task, action)
        self._verify(binding, action)

        resources = binding["resources"]

        preflight_result = self.preflight.run(
            replication_instance_arn=resources[
                "replication_instance_arn"
            ],
            source_endpoint_arn=resources["source_endpoint_arn"],
            credential_sync_function_arn=(
                self.credential_sync_function_arn
            ),
        )

        if preflight_result.get("connectionStatus") != "successful":
            raise TaskOperationBlocked(
                "Source preflight has not succeeded"
            )

        task = self.inspection.task_status(binding)
        observed = self._observed(action, task)

        if observed is not None:
            return self._record_observation(
                binding,
                request,
                observation,
                observed,
            )

        self._require_start_state(task, action)
        self._verify(binding, action)

        admission = self._reserve(binding, observation)

        if admission is not None and not admission.may_dispatch:
            return self._saved_result(admission)

        if admission is not None:
            self._verify(binding, action)

        try:
            response = self.dms.start_replication_task(
                ReplicationTaskArn=task["replication_task_arn"],
                StartReplicationTaskType=(
                    "start-replication"
                    if action == "start"
                    else "resume-processing"
                ),
            )

        except ClientError as error:
            if self.operations is not None:
                raise

            return self._reconcile_or_raise(
                binding,
                action,
                error,
            )

        result = self._requested(action, task, response)
        result["preflight"] = preflight_result

        return self._finish(
            binding,
            request,
            admission,
            result,
        )

    @staticmethod
    def _require_start_state(
        task: dict[str, Any],
        action: str,
    ) -> None:
        if action == "start":
            allowed = (
                task["status"] == "ready"
                and task.get("has_started") is False
            )

        else:
            allowed = (
                task["status"] == "stopped"
                and task.get("has_started") is True
            )

            if task["migration_type"] == "full-load-and-cdc":
                allowed = allowed and (
                    task.get("full_load_progress_percent") == 100
                    and all(
                        task.get(field) == 0
                        for field in (
                            "tables_loading",
                            "tables_queued",
                            "tables_errored",
                        )
                    )
                )

        if not allowed:
            raise TaskOperationBlocked(
                f"Cannot {action} task in its observed state "
                "without resetting or reloading it"
            )

    def stop(
        self,
        binding: dict[str, Any],
        *,
        request: RuntimeRequest | None = None,
    ) -> dict[str, Any]:
        previous, observation = self._prepare(
            binding,
            "stop",
            request,
        )

        if previous is not None:
            return previous

        self._verify(binding, "stop")
        task = self.inspection.task_status(binding)

        observed = self._observed("stop", task)

        if observed is not None:
            return self._record_observation(
                binding,
                request,
                observation,
                observed,
            )

        if task["status"] != "running":
            raise TaskOperationBlocked(
                "Stop requires a running task"
            )

        self._verify(binding, "stop")
        admission = self._reserve(binding, observation)

        if admission is not None and not admission.may_dispatch:
            return self._saved_result(admission)

        if admission is not None:
            self._verify(binding, "stop")

        try:
            response = self.dms.stop_replication_task(
                ReplicationTaskArn=task["replication_task_arn"],
            )

        except ClientError as error:
            if self.operations is not None:
                raise

            return self._reconcile_or_raise(
                binding,
                "stop",
                error,
            )

        return self._finish(
            binding,
            request,
            admission,
            self._requested("stop", task, response),
        )

    def _reconcile_or_raise(
        self,
        binding: dict[str, Any],
        action: str,
        error: ClientError,
    ) -> dict[str, Any]:
        if (
            error.response["Error"]["Code"]
            == "InvalidResourceStateFault"
        ):
            task = self.inspection.task_status(binding)
            observed = self._observed(action, task)

            if observed is not None:
                return observed

        raise error

    @staticmethod
    def _requested(
        action: str,
        task: dict[str, Any],
        response: dict[str, Any],
    ) -> dict[str, Any]:
        actual = response.get("ReplicationTask", {})

        if (
            actual.get("ReplicationTaskArn")
            != task["replication_task_arn"]
        ):
            raise InspectionError(
                "DMS operation returned a different task; "
                "reconcile its outcome"
            )

        status = actual.get("Status")

        if not isinstance(status, str) or not status:
            raise InspectionError(
                "DMS operation returned no task status; "
                "reconcile its outcome"
            )

        return {
            "action": action,
            "replication_task_arn": task["replication_task_arn"],
            "task_status": status,
            "outcome": "REQUESTED",
        }