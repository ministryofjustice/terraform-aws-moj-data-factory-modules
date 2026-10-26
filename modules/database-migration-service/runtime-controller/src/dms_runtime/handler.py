from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from dms_runtime.full_load import FullLoadReadiness
from dms_runtime.history import RuntimeOperationHistory
from dms_runtime.inspection import TaskInspection
from dms_runtime.lookup import PinnedRuntimeLookup
from dms_runtime.reconciliation import RuntimeReconciliation
from dms_runtime.requests import (
    OWNED_ACTIONS,
    RuntimeRequest,
)


OwnedActionHandler = Callable[
    [RuntimeRequest, dict[str, Any]],
    dict[str, Any],
]


class RuntimeHandler:
    def __init__(
        self,
        *,
        lookup: PinnedRuntimeLookup,
        inspection: TaskInspection,
        full_load: FullLoadReadiness,
        owned_action_handler: OwnedActionHandler,
    ) -> None:
        if not callable(owned_action_handler):
            raise ValueError(
                "An owned-action handler is required"
            )

        self.lookup = lookup
        self.inspection = inspection
        self.full_load = full_load
        self.owned_action_handler = owned_action_handler
        self.history = RuntimeOperationHistory(lookup)
        self.reconciliation = RuntimeReconciliation(
            history=self.history,
            inspection=inspection,
        )

    def handle(self, event: Any) -> dict[str, Any]:
        request = RuntimeRequest.from_event(event)

        if request.action == "full_load_status":
            return self._full_load_status(request)

        if request.action == "operation_status":
            return self.reconciliation.inspect(
                ingestion_id=request.ingestion_id,
                operation_id=request.target_operation_id,
            )

        binding = self.lookup.resolve(request.ingestion_id)

        if request.action == "task_status":
            return self.inspection.task_status(binding)

        if request.action == "table_status":
            return self.inspection.table_status_page(
                binding,
                marker=request.marker,
            )

        if request.action in OWNED_ACTIONS:
            return self.owned_action_handler(
                request,
                binding,
            )

        raise ValueError(
            "The runtime action has no configured handler"
        )

    def _full_load_status(
        self,
        request: RuntimeRequest,
    ) -> dict[str, Any]:
        binding, boundary = self.history.full_load_start(
            ingestion_id=request.ingestion_id,
            operation_id=request.start_operation_id,
        )

        selected = (
            None
            if request.selected_tables is None
            else [
                asdict(table)
                for table in request.selected_tables
            ]
        )

        result = self.full_load.assess(
            binding,
            full_load_not_before=boundary,
            selected_tables=selected,
        )

        return {
            **result,
            "start_operation_id": request.start_operation_id,
        }