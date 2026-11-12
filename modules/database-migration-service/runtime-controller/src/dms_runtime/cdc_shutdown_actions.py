from collections.abc import Mapping
from typing import Any

from dms_runtime.batch_downstream import BatchPrerequisiteMissing
from dms_runtime.batch_processing import _BatchStage
from ingestion_registry.progress import ProgressConflict
from ingestion_registry.references import S3ObjectReference


class _CdcShutdownStage(_BatchStage):
    predecessor: str

    def _build_request(self, ingestion_id: str) -> dict[str, Any]:
        stopped = self._read_evidence(ingestion_id, "SOURCE_STOPPED")
        prior = self._read_evidence(ingestion_id, self.predecessor)

        for field in ("task_arn", "boundary_id"):
            if prior[field] != stopped[field]:
                raise ProgressConflict(
                    f"Ingestion {ingestion_id}: {self.predecessor} "
                    "does not match the recorded source-stop boundary"
                )

        return {
            "ingestion_id": ingestion_id,
            "task_arn": stopped["task_arn"],
            "boundary_id": stopped["boundary_id"],
            "prerequisite": dict(prior),
        }

    def _read_evidence(
        self, ingestion_id: str, stage: str
    ) -> Mapping[str, str]:
        record = self._progress.read_stage(
            ingestion_id=ingestion_id,
            stage=stage,
        )
        if record is None or record["status"] != "SUCCEEDED":
            raise BatchPrerequisiteMissing(
                f"Ingestion {ingestion_id}: {stage} must succeed "
                f"before {self.stage}"
            )

        reference = record.get("result_reference")
        if (
            not isinstance(reference, Mapping)
            or reference.get("kind") != "cdc-shutdown-evidence"
            or reference.get("stage") != stage
            or reference.get("ingestion_id") != ingestion_id
            or any(
                not isinstance(reference.get(field), str)
                or not reference[field].strip()
                for field in ("task_arn", "boundary_id")
            )
        ):
            raise ProgressConflict(
                f"Ingestion {ingestion_id}: {stage} has invalid shutdown evidence"
            )

        _check_object_reference(reference)
        return reference

    def _validate_result(
        self,
        request: Mapping[str, Any],
        reference: Mapping[str, str],
    ) -> None:
        expected = {
            "kind": "cdc-shutdown-evidence",
            "stage": self.stage,
            "ingestion_id": request["ingestion_id"],
            "task_arn": request["task_arn"],
            "boundary_id": request["boundary_id"],
        }
        if any(reference.get(field) != value for field, value in expected.items()):
            raise ProgressConflict(
                f"{self.stage} evidence does not match the recorded "
                "source-stop boundary"
            )

        _check_object_reference(reference)


def _check_object_reference(reference: Mapping[str, str]) -> None:
    try:
        S3ObjectReference(
            bucket_name=reference["bucket_name"],
            object_key=reference["object_key"],
            version_id=reference["version_id"],
            sha256=reference["sha256"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ProgressConflict(
            "CDC shutdown evidence requires a versioned object reference"
        ) from error


class CdcConsumerStop(_CdcShutdownStage):
    """Stop the consumer after drain has been recorded."""

    stage = "CDC_CONSUMER_STOPPED"
    predecessor = "CDC_DRAINED"
    operation_kind = "cdc-consumer-stop-operation"
    update_prefix = "cdc-consumer-stop"


class CdcHistoryFinalisation(_CdcShutdownStage):
    """Finalise Raw History after consumer stop has been recorded."""

    stage = "HISTORY_FINALIZED"
    predecessor = "CDC_CONSUMER_STOPPED"
    operation_kind = "cdc-history-finalisation-operation"
    update_prefix = "cdc-history-finalisation"