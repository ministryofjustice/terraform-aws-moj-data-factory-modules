from dataclasses import asdict

from dms_runtime.full_load_raw_batch import (
    DmsParquetFullLoadSelector,
    FullLoadRawBatchHandoff,
)
from dms_runtime.raw_batch_manifest import RawBatchManifestProducer
from ingestion_registry.progress import IngestionProgress


class TaskGuardedProgress(IngestionProgress):
    def __init__(self, guard, binding):
        super().__init__(
            guard.registry,
            clock=guard.clock,
        )
        self.guard = guard
        self.binding = binding

    def _guards(self, claim, now):
        if claim != self.guard.ingestion_claim:
            raise ValueError(
                "Progress claim differs from the runtime owner"
            )

        return self.guard.transaction_checks(
            self.binding,
            "commit_raw_batch",
        )


class RawBatchAction:
    def __init__(
        self,
        *,
        client,
        history,
        readiness,
        manifest_location,
        allowed_landings,
    ):
        self.client = client
        self.history = history
        self.readiness = readiness
        self.manifest_location = dict(manifest_location)
        self.allowed_landings = tuple(
            dict(item)
            for item in allowed_landings
        )

    def __call__(self, request, binding, guard):
        if request.action != "commit_raw_batch":
            raise ValueError("Expected commit_raw_batch")

        if binding["landing"] not in self.allowed_landings:
            raise PermissionError(
                "Raw landing is outside the configured locations"
            )

        guard(binding, request.action)

        progress = TaskGuardedProgress(
            guard,
            binding,
        )

        producer = RawBatchManifestProducer(
            client=self.client,
            progress=progress,
            **self.manifest_location,
        )

        handoff = FullLoadRawBatchHandoff(
            history=self.history,
            readiness=self.readiness,
            selector=DmsParquetFullLoadSelector(
                self.client,
                bucket_owner=guard.context.account_id,
            ),
            producer=producer,
        )

        reference = handoff.advance(
            guard.ingestion_claim,
            start_operation_id=request.start_operation_id,
        )

        guard(binding, request.action)

        return {
            "action": request.action,
            "ingestion_id": request.ingestion_id,
            "operation_id": request.operation_id,
            "start_operation_id": request.start_operation_id,
            "outcome": "SUCCEEDED",
            "raw_batch_manifest": {
                "kind": "raw-batch",
                **asdict(reference),
            },
        }