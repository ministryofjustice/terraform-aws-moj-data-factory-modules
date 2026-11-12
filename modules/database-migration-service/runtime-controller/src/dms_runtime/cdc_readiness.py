import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from dms_runtime.batch_downstream import BatchPrerequisiteMissing
from ingestion_registry.extraction_policy import resolve_extraction_policy
from ingestion_registry.ownership import OwnershipClaim
from ingestion_registry.progress import IngestionProgress, ProgressConflict
from ingestion_registry.references import S3ObjectReference


@dataclass(frozen=True)
class CdcReadinessEvidence:
    ingestion_id: str
    task_arn: str
    start_boundary_id: str
    reference: S3ObjectReference


class CdcReadinessProbe(Protocol):
    def read_readiness_evidence(
        self,
        *,
        ingestion_id: str,
        task_arn: str,
        start_boundary_id: str,
    ) -> CdcReadinessEvidence | None:
        """Return verified readiness at the requested start boundary."""


class CdcReadiness:
    def __init__(
        self,
        *,
        progress: IngestionProgress,
        resolve_deployment: Callable[[str], Mapping[str, Any]],
        resolve_start_boundary: Callable[[str], str | None],
        probe: CdcReadinessProbe,
    ) -> None:
        self._progress = progress
        self._resolve_deployment = resolve_deployment
        self._resolve_start_boundary = resolve_start_boundary
        self._probe = probe

    def advance(self, claim: OwnershipClaim) -> dict[str, Any]:
        ingestion_id = claim.ingestion_id
        deployment = self._resolve_deployment(ingestion_id)
        if not resolve_extraction_policy(deployment).includes_cdc:
            raise BatchPrerequisiteMissing(
                f"Ingestion {ingestion_id}: consumer readiness requires CDC"
            )

        task_arn = deployment["resources"]["replication_task_arn"]
        start_boundary_id = self._resolve_start_boundary(ingestion_id)
        if (
            not isinstance(start_boundary_id, str)
            or not start_boundary_id
            or start_boundary_id != start_boundary_id.strip()
            or len(start_boundary_id.encode("utf-8")) > 2048
        ):
            raise BatchPrerequisiteMissing(
                f"Ingestion {ingestion_id}: no approved CDC start boundary"
            )

        identity = {
            "kind": "cdc-consumer-readiness",
            "ingestion_id": ingestion_id,
            "task_arn": task_arn,
            "start_boundary_id": start_boundary_id,
        }
        current = self._progress.read_stage(
            ingestion_id=ingestion_id,
            stage="CDC_CONSUMER",
        )

        if current is not None and current["status"] == "SUCCEEDED":
            reference = current.get("result_reference")
            if not isinstance(reference, Mapping) or any(
                reference.get(field) != value for field, value in identity.items()
            ):
                raise ProgressConflict(
                    "Recorded consumer readiness differs from the approved CDC start"
                )
            return current

        if current is not None and current["status"] != "RUNNING":
            raise ProgressConflict(
                f"Ingestion {ingestion_id}: CDC_CONSUMER requires recovery"
            )

        evidence = self._probe.read_readiness_evidence(
            ingestion_id=ingestion_id,
            task_arn=task_arn,
            start_boundary_id=start_boundary_id,
        )
        if evidence is None:
            return {
                "ingestion_id": ingestion_id,
                "stage": "CDC_CONSUMER",
                "status": "RUNNING",
            }

        if (
            not isinstance(evidence, CdcReadinessEvidence)
            or not isinstance(evidence.reference, S3ObjectReference)
            or (
                evidence.ingestion_id,
                evidence.task_arn,
                evidence.start_boundary_id,
            )
            != (ingestion_id, task_arn, start_boundary_id)
        ):
            raise ProgressConflict(
                "Consumer evidence does not match the approved CDC start"
            )

        revision = int(current["revision"]) if current is not None else 0
        reference = {**identity, **asdict(evidence.reference)}
        update_digest = hashlib.sha256(
            json.dumps(
                {"revision": revision, "reference": reference},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

        return self._progress.record_stage(
            claim,
            stage="CDC_CONSUMER",
            status="SUCCEEDED",
            update_id=f"cdc-ready-{update_digest}",
            expected_revision=revision,
            result_reference=reference,
        )