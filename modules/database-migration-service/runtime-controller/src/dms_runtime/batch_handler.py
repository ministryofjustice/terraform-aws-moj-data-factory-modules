from collections.abc import Mapping
from typing import Any

from dms_runtime.batch_processing import (
    BatchProcessing,
    BatchPublication,
    BatchRawHistory,
)
from ingestion_registry.lifecycle import IngestionLifecycle
from ingestion_registry.ownership import OwnershipClaim


class BatchHandler:
    """Advance one downstream action for an owned ingestion."""

    def __init__(
        self,
        *,
        processing: BatchProcessing,
        raw_history: BatchRawHistory,
        publication: BatchPublication,
        lifecycle: IngestionLifecycle,
        reconciliation_required: bool,
    ) -> None:
        if not isinstance(reconciliation_required, bool):
            raise ValueError("reconciliation_required must be a boolean")

        self._stage_actions = {
            "advance_processing": processing.advance,
            "advance_raw_history": raw_history.advance,
            "advance_publication": publication.advance,
        }
        self._lifecycle = lifecycle
        self._reconciliation_required = reconciliation_required

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

        advance = self._stage_actions.get(action)
        if advance is None:
            raise ValueError(f"Unsupported batch action: {action!r}")

        result = advance(claim)
        return {
            "ingestion_id": ingestion_id,
            "stage": result["stage"],
            "status": result["status"],
        }