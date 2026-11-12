from collections.abc import Mapping
from typing import Any

from dms_runtime.batch_downstream import BatchPrerequisiteMissing
from dms_runtime.batch_processing import (
    BatchProcessing,
    BatchPublication,
    BatchRawHistory,
    BatchReconciliation,
)
from dms_runtime.cdc_drain import CdcDrain
from dms_runtime.cdc_readiness import CdcReadiness
from ingestion_registry.lifecycle import IngestionLifecycle
from ingestion_registry.ownership import OwnershipClaim
from dms_runtime.cdc_shutdown_actions import (
    CdcConsumerStop,
    CdcHistoryFinalisation,
)


class BatchHandler:
    def __init__(
        self,
        *,
        processing: BatchProcessing,
        raw_history: BatchRawHistory,
        publication: BatchPublication,
        lifecycle: IngestionLifecycle,
        reconciliation_required: bool,
        reconciliation: BatchReconciliation | None = None,
        cdc_readiness: CdcReadiness | None = None,
        cdc_drain: CdcDrain | None = None,
        cdc_consumer_stop: CdcConsumerStop | None = None,
        cdc_history_finalisation: CdcHistoryFinalisation | None = None,
    ) -> None:
        if not isinstance(reconciliation_required, bool):
            raise ValueError("reconciliation_required must be a boolean")

        self._stage_actions = {
            "advance_processing": processing.advance,
            "advance_raw_history": raw_history.advance,
            "advance_publication": publication.advance,
        }
        self._cdc_actions = {
            "advance_cdc_readiness": cdc_readiness,
            "advance_cdc_drain": cdc_drain,
            "advance_cdc_consumer_stop": cdc_consumer_stop,
            "advance_cdc_history_finalisation": cdc_history_finalisation,
        }
        self._lifecycle = lifecycle
        self._reconciliation_required = reconciliation_required
        self._reconciliation = reconciliation

    def handle(
        self,
        request: Mapping[str, Any],
        *,
        claim: OwnershipClaim,
    ) -> dict[str, str]:
        if set(request) != {"request_version", "action", "ingestion_id"}:
            raise ValueError(
                "Batch requests require request_version, action and ingestion_id"
            )

        version = request["request_version"]
        if type(version) is not int or version != 1:
            raise ValueError("request_version must be the integer 1")

        ingestion_id = request["ingestion_id"]
        if (
            not isinstance(ingestion_id, str)
            or not ingestion_id
            or ingestion_id != claim.ingestion_id
        ):
            raise ValueError("ingestion_id must match the ownership claim")

        action = request["action"]
        if not isinstance(action, str):
            raise ValueError("action must be a string")

        if action == "mark_baseline_ready":
            self._lifecycle.mark_baseline_ready(
                claim,
                reconciliation_required=self._reconciliation_required,
            )
            return {
                "ingestion_id": ingestion_id,
                "stage": "BASELINE",
                "status": "SUCCEEDED",
            }

        if action == "advance_reconciliation":
            if not self._reconciliation_required:
                return {
                    "ingestion_id": ingestion_id,
                    "stage": "RECONCILIATION",
                    "status": "NOT_REQUIRED",
                }
            if self._reconciliation is None:
                raise BatchPrerequisiteMissing(
                    f"Ingestion {ingestion_id}: required reconciliation "
                    "adapter is unavailable"
                )
            advance = self._reconciliation.advance
        elif action in self._cdc_actions:
            operation = self._cdc_actions[action]
            if operation is None:
                raise BatchPrerequisiteMissing(
                    f"Ingestion {ingestion_id}: {action} is not configured"
                )
            advance = operation.advance
        else:
            advance = self._stage_actions.get(action)
            if advance is None:
                raise ValueError(f"Unsupported batch action: {action!r}")

        result = advance(claim)
        return {
            "ingestion_id": ingestion_id,
            "stage": result["stage"],
            "status": result["status"],
        }