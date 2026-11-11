from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ingestion_registry.references import S3ObjectReference


class BatchPrerequisiteMissing(RuntimeError):
    """The ingestion does not have the evidence needed to continue."""


@dataclass(frozen=True)
class BatchInputs:
    ingestion_id: str
    deployment: Mapping[str, Any]
    contract: Mapping[str, str]
    raw_batch: S3ObjectReference


class BatchDownstream:
    """Read the pinned inputs and stage evidence for downstream batch work."""

    def __init__(
        self,
        *,
        resolve_deployment: Callable[[str], Mapping[str, Any]],
        read_stage: Callable[..., dict[str, Any] | None],
    ) -> None:
        self._resolve_deployment = resolve_deployment
        self._read_stage = read_stage

    def load_inputs(self, ingestion_id: str) -> BatchInputs:
        deployment = self._resolve_deployment(ingestion_id)
        contract = self._completed_result(ingestion_id, "CONTRACT")

        if contract.get("kind") != "registered-contract":
            raise BatchPrerequisiteMissing(
                f"Ingestion {ingestion_id}: CONTRACT must reference "
                "a registered contract"
            )

        extraction = self._completed_result(ingestion_id, "EXTRACTION")
        if extraction.get("kind") != "raw-batch":
            raise BatchPrerequisiteMissing(
                f"Ingestion {ingestion_id}: EXTRACTION must identify "
                "the completed Raw batch"
            )

        try:
            raw_batch = S3ObjectReference(
                bucket_name=extraction["bucket_name"],
                object_key=extraction["object_key"],
                version_id=extraction["version_id"],
                sha256=extraction["sha256"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise BatchPrerequisiteMissing(
                f"Ingestion {ingestion_id}: EXTRACTION has an invalid "
                "Raw batch reference"
            ) from error

        return BatchInputs(
            ingestion_id=ingestion_id,
            deployment=deployment,
            contract=contract,
            raw_batch=raw_batch,
        )

    def publication_prerequisites(
        self,
        ingestion_id: str,
        *,
        reconciliation_required: bool,
    ) -> dict[str, dict[str, str]]:
        if not isinstance(reconciliation_required, bool):
            raise ValueError("reconciliation_required must be a boolean")

        stages = ["PROCESSING", "HISTORY_BASELINE"]
        if reconciliation_required:
            stages.append("RECONCILIATION")

        return {
            stage: self._completed_result(ingestion_id, stage)
            for stage in stages
        }

    def _completed_result(
        self,
        ingestion_id: str,
        stage: str,
    ) -> dict[str, str]:
        record = self._read_stage(
            ingestion_id=ingestion_id,
            stage=stage,
        )
        if record is None or record.get("status") != "SUCCEEDED":
            raise BatchPrerequisiteMissing(
                f"Ingestion {ingestion_id}: {stage} has not succeeded"
            )

        reference = record.get("result_reference")
        if not isinstance(reference, dict) or not reference or any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            or not value
            for key, value in reference.items()
        ):
            raise BatchPrerequisiteMissing(
                f"Ingestion {ingestion_id}: {stage} has no valid "
                "durable result reference"
            )

        return dict(reference)