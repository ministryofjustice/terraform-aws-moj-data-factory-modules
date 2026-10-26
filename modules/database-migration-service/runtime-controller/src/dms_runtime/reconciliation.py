from typing import Any

from ingestion_registry.identity import identity_digest

from dms_runtime.history import RuntimeOperationHistory
from dms_runtime.inspection import TaskInspection
from dms_runtime.operations import RuntimeOperationConflict


class RuntimeReconciliation:
    def __init__(
        self,
        *,
        history: RuntimeOperationHistory,
        inspection: TaskInspection,
    ) -> None:
        self.history = history
        self.inspection = inspection

    def inspect(
        self,
        *,
        ingestion_id: str,
        operation_id: str,
    ) -> dict[str, Any]:
        stored = self.history.get(
            ingestion_id=ingestion_id,
            operation_id=operation_id,
        )

        record = stored.record
        registry = self.history.registry

        reference = {
            "table_name": registry.table_name,
            "partition_key": record["pk"],
            "sort_key": record["sk"],
        }

        common = {
            "ingestion_id": ingestion_id,
            "operation_id": operation_id,
            "action": record["action"],
            "replication_task_arn": record[
                "replication_task_arn"
            ],
            "operation_reference": reference,
            "dispatch_permitted": False,
        }

        if record.get("status") == "RECORDED":
            return {
                **common,
                "reconciliation_status": "RESULT_RECORDED",
            }

        if record.get("status") != "DISPATCHING":
            raise RuntimeOperationConflict(
                "The runtime operation has an unsupported status"
            )

        task_key = "DMSTASK#" + identity_digest(
            [record["replication_task_arn"]]
        )

        gate = registry._read(
            task_key,
            "RUNTIME_COMMAND",
        )

        gate_matches = (
            gate is not None
            and gate.get("operation_pk") == record["pk"]
            and gate.get("operation_sk") == record["sk"]
            and gate.get("dispatch_token")
            == record.get("dispatch_token")
        )

        task = self.inspection.task_status(stored.binding)

        return {
            **common,
            "reconciliation_status": "BLOCKED",
            "reason": "COMMAND_OUTCOME_UNKNOWN",
            "command_gate": (
                "HELD"
                if gate_matches
                else "MISSING_OR_CHANGED"
            ),
            "task_observation": task,
            "automatic_resolution_available": False,
        }