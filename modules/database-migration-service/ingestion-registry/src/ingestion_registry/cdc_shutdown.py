from dataclasses import asdict, dataclass
from typing import Any

from ingestion_registry.extraction_policy import (
    resolve_extraction_policy,
)
from ingestion_registry.ownership import OwnershipClaim
from ingestion_registry.progress import IngestionProgress
from ingestion_registry.references import S3ObjectReference


SHUTDOWN_STAGES = frozenset(
    {
        "SOURCE_STOPPED",
        "CDC_DRAINED",
        "HISTORY_FINALIZED",
    }
)


@dataclass(frozen=True)
class CdcShutdownEvidence:
    ingestion_id: str
    task_arn: str
    boundary_id: str
    reference: S3ObjectReference

    def __post_init__(self) -> None:
        for field in (
            "ingestion_id",
            "task_arn",
            "boundary_id",
        ):
            value = getattr(self, field)

            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or len(value.encode("utf-8")) > 2048
            ):
                raise ValueError(
                    f"{field} must contain 1 to 2048 UTF-8 bytes "
                    "without surrounding whitespace"
                )

        if not isinstance(self.reference, S3ObjectReference):
            raise ValueError(
                "reference must be an S3ObjectReference"
            )


class CdcShutdown:
    def __init__(
        self,
        progress: IngestionProgress,
    ) -> None:
        self.progress = progress
        self.registry = progress.registry

    def record_evidence(
        self,
        claim: OwnershipClaim,
        *,
        stage: str,
        evidence: CdcShutdownEvidence,
        update_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        if stage not in SHUTDOWN_STAGES:
            raise ValueError(
                f"Unsupported CDC shutdown stage: {stage!r}"
            )

        if not isinstance(evidence, CdcShutdownEvidence):
            raise ValueError(
                "evidence must be CdcShutdownEvidence"
            )

        if evidence.ingestion_id != claim.ingestion_id:
            raise ValueError(
                "Evidence ingestion_id does not match the claim"
            )

        pinned = self.registry._read(
            f"INGESTION#{claim.ingestion_id}",
            "BINDING",
        )

        if pinned is None:
            raise ValueError(
                "CDC shutdown requires a pinned resource binding"
            )

        binding = pinned["binding"]
        policy = resolve_extraction_policy(binding)

        if not policy.includes_cdc:
            raise ValueError(
                "CDC shutdown requires a CDC-enabled binding"
            )

        if evidence.task_arn != binding["resources"][
            "replication_task_arn"
        ]:
            raise ValueError(
                "Evidence task_arn does not match the pinned task"
            )

        if stage != "SOURCE_STOPPED":
            stopped = self.progress.read_stage(
                ingestion_id=claim.ingestion_id,
                stage="SOURCE_STOPPED",
            )

            if stopped is None or stopped["status"] != "SUCCEEDED":
                raise ValueError(
                    "SOURCE_STOPPED must succeed before "
                    "recording drain or final history evidence"
                )

            stopped_reference = stopped["result_reference"]

            if (
                stopped_reference["boundary_id"]
                != evidence.boundary_id
                or stopped_reference["task_arn"]
                != evidence.task_arn
            ):
                raise ValueError(
                    "Evidence does not match the recorded "
                    "source shutdown boundary"
                )

        reference = {
            "kind": "cdc-shutdown-evidence",
            "stage": stage,
            "ingestion_id": evidence.ingestion_id,
            "task_arn": evidence.task_arn,
            "boundary_id": evidence.boundary_id,
            **asdict(evidence.reference),
        }

        return self.progress.record_stage(
            claim,
            stage=stage,
            status="SUCCEEDED",
            update_id=update_id,
            expected_revision=expected_revision,
            result_reference=reference,
        )