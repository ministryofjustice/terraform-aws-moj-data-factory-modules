import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Protocol
from uuid import uuid4

from dms_runtime.batch_downstream import BatchDownstream
from ingestion_registry.ownership import OwnershipClaim
from ingestion_registry.progress import IngestionProgress, ProgressConflict


@dataclass(frozen=True)
class ProcessingCompletion:
    operation_id: str
    ingestion_id: str
    request_fingerprint: str
    result_reference: Mapping[str, str]


class ProcessingAdapter(Protocol):
    def submit(
        self,
        *,
        operation_id: str,
        request_fingerprint: str,
        request: Mapping[str, Any],
    ) -> ProcessingCompletion | None:
        """Ensure the pinned operation is accepted and return verified completion.

        Accept an operation that is absent. Reuse an identical accepted
        operation without starting another side effect. Reject conflicting
        inputs and return None while completion is pending.
        """

    def lookup(
        self,
        *,
        operation_id: str,
    ) -> ProcessingCompletion | None:
        """Return verified completion, or None when it cannot yet be confirmed."""


def _fingerprint(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class _BatchStage:
    stage: str
    operation_kind: str
    update_prefix: str
    required_result_fields: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        downstream: BatchDownstream,
        progress: IngestionProgress,
        adapter: ProcessingAdapter,
    ) -> None:
        self._downstream = downstream
        self._progress = progress
        self._adapter = adapter

    def advance(self, claim: OwnershipClaim) -> dict[str, Any]:
        ingestion_id = claim.ingestion_id
        current = self._progress.read_stage(
            ingestion_id=ingestion_id,
            stage=self.stage,
        )

        if current is not None and current["status"] == "SUCCEEDED":
            return current

        if current is not None and current["status"] != "RUNNING":
            raise ProgressConflict(
                f"Ingestion {ingestion_id}: {self.stage} requires "
                "an explicit retry decision"
            )

        request = self._build_request(ingestion_id)
        request_fingerprint = _fingerprint(request)
        operation_id = _fingerprint(
            {"ingestion_id": ingestion_id, "stage": self.stage}
        )
        reservation = {
            "kind": self.operation_kind,
            "operation_id": operation_id,
            "request_fingerprint": request_fingerprint,
        }

        if current is None:
            current = self._progress.record_stage(
                claim,
                stage=self.stage,
                status="RUNNING",
                update_id=f"{self.update_prefix}-reserve-{uuid4().hex}",
                expected_revision=0,
                result_reference=reservation,
            )
            completion = None
        else:
            if current.get("result_reference") != reservation:
                raise ProgressConflict(
                    f"Ingestion {ingestion_id}: {self.stage} reservation "
                    "does not match the pinned request"
                )

            completion = self._adapter.lookup(operation_id=operation_id)

        if completion is None:
            completion = self._adapter.submit(
                operation_id=operation_id,
                request_fingerprint=request_fingerprint,
                request=request,
            )

        if completion is None:
            return current

        if (
            completion.operation_id != operation_id
            or completion.ingestion_id != ingestion_id
            or completion.request_fingerprint != request_fingerprint
            or not completion.result_reference
        ):
            raise ProgressConflict(
                f"Ingestion {ingestion_id}: {self.stage} completion "
                "does not match the reserved operation"
            )

        for field in self.required_result_fields:
            value = completion.result_reference.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ProgressConflict(
                    f"Ingestion {ingestion_id}: {self.stage} completion "
                    f"requires {field}"
                )

        self._validate_result(request, completion.result_reference)

        return self._progress.record_stage(
            claim,
            stage=self.stage,
            status="SUCCEEDED",
            update_id=f"{self.update_prefix}-complete-{uuid4().hex}",
            expected_revision=int(current["revision"]),
            result_reference={
                **completion.result_reference,
                "operation_id": operation_id,
                "request_fingerprint": request_fingerprint,
            },
        )

    def _validate_result(
        self,
        request: Mapping[str, Any],
        reference: Mapping[str, str],
    ) -> None:
        """Allow a stage to verify its evidence before recording success."""

    def _build_request(self, ingestion_id: str) -> dict[str, Any]:
        inputs = self._downstream.load_inputs(ingestion_id)
        return {
            "ingestion_id": ingestion_id,
            "deployment": dict(inputs.deployment),
            "contract": dict(inputs.contract),
            "raw_batch": asdict(inputs.raw_batch),
        }


class BatchProcessing(_BatchStage):
    stage = "PROCESSING"
    operation_kind = "processing-operation"
    update_prefix = "processing"


class BatchRawHistory(_BatchStage):
    stage = "HISTORY_BASELINE"
    operation_kind = "raw-history-operation"
    update_prefix = "raw-history"
    required_result_fields = ("commit_id", "history_location")


class BatchPublication(_BatchStage):
    stage = "PUBLICATION"
    operation_kind = "publication-operation"
    update_prefix = "publication"
    required_result_fields = ("glue_database", "glue_table")

    def __init__(
        self,
        *,
        downstream: BatchDownstream,
        progress: IngestionProgress,
        adapter: ProcessingAdapter,
        reconciliation_required: bool,
    ) -> None:
        super().__init__(
            downstream=downstream,
            progress=progress,
            adapter=adapter,
        )
        if not isinstance(reconciliation_required, bool):
            raise ValueError("reconciliation_required must be a boolean")
        self._reconciliation_required = reconciliation_required

    def _build_request(self, ingestion_id: str) -> dict[str, Any]:
        prerequisites = self._downstream.publication_prerequisites(
            ingestion_id,
            reconciliation_required=self._reconciliation_required,
        )
        history = prerequisites["HISTORY_BASELINE"]

        history_fields = (
            "commit_id",
            "history_location",
            "bucket_name",
            "object_key",
            "version_id",
            "sha256",
        )
        for field in history_fields:
            if not history.get(field, "").strip():
                raise ProgressConflict(
                    f"Ingestion {ingestion_id}: HISTORY_BASELINE "
                    f"completion requires {field} before PUBLICATION"
                )

        inputs = self._downstream.load_inputs(ingestion_id)
        return {
            "ingestion_id": ingestion_id,
            "deployment": dict(inputs.deployment),
            "contract": dict(inputs.contract),
            "history_commit_id": history["commit_id"],
            "history_location": history["history_location"],
            "history_commit": {
                "bucket_name": history["bucket_name"],
                "object_key": history["object_key"],
                "version_id": history["version_id"],
                "sha256": history["sha256"],
            },
        }


class BatchReconciliation(_BatchStage):
    stage = "RECONCILIATION"
    operation_kind = "reconciliation-operation"
    update_prefix = "reconciliation"
    required_result_fields = ("check_id",)

    def _build_request(self, ingestion_id: str) -> dict[str, Any]:
        prerequisites = self._downstream.publication_prerequisites(
            ingestion_id,
            reconciliation_required=False,
        )
        inputs = self._downstream.load_inputs(ingestion_id)
        return {
            "ingestion_id": ingestion_id,
            "deployment": dict(inputs.deployment),
            "contract": dict(inputs.contract),
            "raw_batch": asdict(inputs.raw_batch),
            "processing_result": prerequisites["PROCESSING"],
            "history_result": prerequisites["HISTORY_BASELINE"],
        }