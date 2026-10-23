from dataclasses import replace

import pytest

from ingestion_registry.cdc_shutdown import (
    CdcShutdown,
    CdcShutdownEvidence,
)
from ingestion_registry.completion import (
    CompletionConflict,
    IngestionCompletion,
)
from ingestion_registry.identity import TableIdentity
from ingestion_registry.lifecycle import (
    BASELINE_STAGES,
    IngestionLifecycle,
)
from ingestion_registry.ownership import OwnershipLost
from ingestion_registry.progress import IngestionProgress
from ingestion_registry.references import S3ObjectReference


SHUTDOWN_STAGES = (
    "SOURCE_STOPPED",
    "CDC_DRAINED",
    "HISTORY_FINALIZED",
)


@pytest.fixture
def cdc_completion_setup(binding_setup):
    (
        registry,
        ownership,
        admitted,
        clock,
        claim,
        bindings,
        binding,
    ) = binding_setup

    pinned = bindings.pin(claim, binding)

    progress = IngestionProgress(
        registry,
        clock=lambda: clock["now"],
    )

    for stage in BASELINE_STAGES:
        progress.record_stage(
            claim,
            stage=stage,
            status="SUCCEEDED",
            update_id=f"baseline-{stage}",
            expected_revision=0,
            result_reference={
                "uri": f"s3://example-evidence/{stage}.json",
                "version_id": "version-one",
            },
        )

    IngestionLifecycle(
        progress,
        clock=lambda: clock["now"],
    ).mark_baseline_ready(
        claim,
        reconciliation_required=False,
    )

    evidence = CdcShutdownEvidence(
        ingestion_id=admitted.ingestion_id,
        task_arn=binding["resources"]["replication_task_arn"],
        boundary_id="final-boundary-one",
        reference=S3ObjectReference(
            bucket_name="example-evidence",
            object_key="shutdown/source.json",
            version_id="version-one",
            sha256="e" * 64,
        ),
    )

    return (
        registry,
        ownership,
        admitted,
        clock,
        claim,
        CdcShutdown(progress),
        evidence,
        IngestionCompletion(
            progress,
            clock=lambda: clock["now"],
        ),
        pinned,
    )


def record_shutdown(shutdown, claim, evidence, stages):
    for stage in stages:
        shutdown.record_evidence(
            claim,
            stage=stage,
            evidence=replace(
                evidence,
                reference=replace(
                    evidence.reference,
                    object_key=f"shutdown/{stage}.json",
                ),
            ),
            update_id=f"shutdown-{stage}",
            expected_revision=0,
        )


def test_cdc_completion_releases_table_and_preserves_boundary(
    cdc_completion_setup,
):
    (
        registry,
        _,
        admitted,
        _,
        claim,
        shutdown,
        evidence,
        completion,
        pinned,
    ) = cdc_completion_setup

    record_shutdown(
        shutdown,
        claim,
        evidence,
        SHUTDOWN_STAGES,
    )

    result = completion.complete_full_load_and_cdc(claim)

    ingestion = registry._read(
        f"INGESTION#{admitted.ingestion_id}",
        "META",
    )
    table = TableIdentity(**ingestion["table_identity"])
    head = registry._read(table.partition_key, "HEAD")

    assert ingestion["status"] == "COMPLETED"
    assert "active_ingestion_id" not in head
    assert result["completion_type"] == "FULL_LOAD_AND_CDC"
    assert set(result["shutdown_evidence"]) == set(SHUTDOWN_STAGES)

    assert registry._read(
        pinned["boundary_key"],
        "RESERVATION",
    )["ingestion_id"] == admitted.ingestion_id


@pytest.mark.parametrize(
    "missing_stage",
    SHUTDOWN_STAGES,
)
def test_incomplete_shutdown_cannot_complete(
    cdc_completion_setup,
    missing_stage,
):
    (
        registry,
        _,
        admitted,
        _,
        claim,
        shutdown,
        evidence,
        completion,
        _,
    ) = cdc_completion_setup

    stages = (
        []
        if missing_stage == "SOURCE_STOPPED"
        else [
            stage
            for stage in SHUTDOWN_STAGES
            if stage != missing_stage
        ]
    )

    record_shutdown(shutdown, claim, evidence, stages)

    with pytest.raises(CompletionConflict, match=missing_stage):
        completion.complete_full_load_and_cdc(claim)

    assert registry._read(
        f"INGESTION#{admitted.ingestion_id}",
        "COMPLETION",
    ) is None


def test_cdc_completion_retry_returns_original_receipt(
    cdc_completion_setup,
):
    (
        _,
        _,
        _,
        clock,
        claim,
        shutdown,
        evidence,
        completion,
        _,
    ) = cdc_completion_setup

    record_shutdown(shutdown, claim, evidence, SHUTDOWN_STAGES)

    first = completion.complete_full_load_and_cdc(claim)
    clock["now"] += 1000
    repeated = completion.complete_full_load_and_cdc(claim)

    assert repeated == first


def test_stale_execution_cannot_complete_cdc(
    cdc_completion_setup,
):
    (
        registry,
        ownership,
        admitted,
        clock,
        claim,
        shutdown,
        evidence,
        completion,
        _,
    ) = cdc_completion_setup

    record_shutdown(shutdown, claim, evidence, SHUTDOWN_STAGES)

    clock["now"] = claim.lease_expires_at
    ownership.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="recovery-execution",
    )

    with pytest.raises(OwnershipLost):
        completion.complete_full_load_and_cdc(claim)

    assert registry._read(
        f"INGESTION#{admitted.ingestion_id}",
        "COMPLETION",
    ) is None


def test_mismatched_stored_boundary_blocks_completion(
    cdc_completion_setup,
):
    (
        registry,
        _,
        admitted,
        _,
        claim,
        shutdown,
        evidence,
        completion,
        _,
    ) = cdc_completion_setup

    record_shutdown(shutdown, claim, evidence, SHUTDOWN_STAGES)

    registry.client.update_item(
        TableName=registry.table_name,
        Key=registry._encode(
            {
                "pk": f"INGESTION#{admitted.ingestion_id}",
                "sk": "STAGE#CDC_DRAINED",
            }
        ),
        UpdateExpression="SET result_reference.boundary_id = :boundary",
        ExpressionAttributeValues=registry._encode(
            {":boundary": "another-boundary"}
        ),
    )

    with pytest.raises(CompletionConflict, match="boundary"):
        completion.complete_full_load_and_cdc(claim)

    assert registry._read(
        f"INGESTION#{admitted.ingestion_id}",
        "COMPLETION",
    ) is None