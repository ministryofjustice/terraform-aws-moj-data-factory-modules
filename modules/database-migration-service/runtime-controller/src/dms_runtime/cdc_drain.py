import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any, Protocol

from dms_runtime.batch_downstream import BatchPrerequisiteMissing
from ingestion_registry.cdc_shutdown import CdcShutdown, CdcShutdownEvidence
from ingestion_registry.ownership import OwnershipClaim
from ingestion_registry.progress import IngestionProgress, ProgressConflict


class CdcDrainProbe(Protocol):
    def read_drain_evidence(
        self,
        *,
        ingestion_id: str,
        task_arn: str,
        boundary_id: str,
    ) -> CdcShutdownEvidence | None:
        """Return verified drain evidence, or None while the consumer is behind."""


class CdcDrain:
    def __init__(
        self,
        *,
        progress: IngestionProgress,
        probe: CdcDrainProbe,
    ) -> None:
        self._progress = progress
        self._shutdown = CdcShutdown(progress)
        self._probe = probe

    def advance(self, claim: OwnershipClaim) -> dict[str, Any]:
        ingestion_id = claim.ingestion_id
        stopped = self._progress.read_stage(
            ingestion_id=ingestion_id,
            stage="SOURCE_STOPPED",
        )
        if stopped is None or stopped["status"] != "SUCCEEDED":
            raise BatchPrerequisiteMissing(
                f"Ingestion {ingestion_id}: SOURCE_STOPPED must succeed before drain"
            )

        stopped_reference = stopped.get("result_reference")
        if not isinstance(stopped_reference, Mapping):
            raise ProgressConflict("SOURCE_STOPPED has no boundary evidence")

        task_arn = stopped_reference.get("task_arn")
        boundary_id = stopped_reference.get("boundary_id")
        if (
            not isinstance(task_arn, str)
            or not task_arn
            or not isinstance(boundary_id, str)
            or not boundary_id
        ):
            raise ProgressConflict("SOURCE_STOPPED has an invalid task or boundary")

        current = self._progress.read_stage(
            ingestion_id=ingestion_id,
            stage="CDC_DRAINED",
        )
        if current is not None:
            if current["status"] == "SUCCEEDED":
                reference = current.get("result_reference") or {}
                if (
                    reference.get("ingestion_id") != ingestion_id
                    or reference.get("task_arn") != task_arn
                    or reference.get("boundary_id") != boundary_id
                ):
                    raise ProgressConflict(
                        "Recorded drain does not match the source-stop boundary"
                    )
                return current

            if current["status"] != "RUNNING":
                raise ProgressConflict(
                    f"Ingestion {ingestion_id}: CDC_DRAINED requires recovery"
                )

        evidence = self._probe.read_drain_evidence(
            ingestion_id=ingestion_id,
            task_arn=task_arn,
            boundary_id=boundary_id,
        )
        if evidence is None:
            return {
                "ingestion_id": ingestion_id,
                "stage": "CDC_DRAINED",
                "status": "RUNNING",
            }

        if not isinstance(evidence, CdcShutdownEvidence) or (
            evidence.ingestion_id,
            evidence.task_arn,
            evidence.boundary_id,
        ) != (ingestion_id, task_arn, boundary_id):
            raise ProgressConflict(
                "Consumer drain evidence does not match the source-stop boundary"
            )

        revision = int(current["revision"]) if current is not None else 0
        identity = json.dumps(
            {"revision": revision, "evidence": asdict(evidence)},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")

        return self._shutdown.record_evidence(
            claim,
            stage="CDC_DRAINED",
            evidence=evidence,
            update_id=f"cdc-drain-{hashlib.sha256(identity).hexdigest()}",
            expected_revision=revision,
        )