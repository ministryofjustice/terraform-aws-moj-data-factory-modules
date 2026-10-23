from dataclasses import replace

import pytest

from ingestion_registry.cdc_shutdown import (
    CdcShutdown,
    CdcShutdownEvidence,
)
from ingestion_registry.ownership import OwnershipLost
from ingestion_registry.progress import IngestionProgress
from ingestion_registry.references import S3ObjectReference


@pytest.fixture
def shutdown_setup(binding_setup):
    (
        registry,
        ownership,
        admitted,
        clock,
        claim,
        bindings,
        binding,
    ) = binding_setup

    bindings.pin(claim, binding)

    progress = IngestionProgress(
        registry,
        clock=lambda: clock["now"],
    )

    shutdown = CdcShutdown(progress)

    evidence = CdcShutdownEvidence(
        ingestion_id=admitted.ingestion_id,
        task_arn=binding["resources"]["replication_task_arn"],
        boundary_id="shutdown-boundary-one",
        reference=S3ObjectReference(
            bucket_name="example-evidence",
            object_key="shutdown/source-stopped.json",
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
        shutdown,
        evidence,
    )


def record_source_stopped(shutdown, claim, evidence):
    return shutdown.record_evidence(
        claim,
        stage="SOURCE_STOPPED",
        evidence=evidence,
        update_id="source-stopped-one",
        expected_revision=0,
    )


def test_shutdown_records_all_three_component_results(
    shutdown_setup,
):
    registry, _, admitted, _, claim, shutdown, evidence = (
        shutdown_setup
    )

    record_source_stopped(shutdown, claim, evidence)

    for stage, object_key in (
        ("CDC_DRAINED", "shutdown/processing-drained.json"),
        ("HISTORY_FINALIZED", "shutdown/history-finalized.json"),
    ):
        result = shutdown.record_evidence(
            claim,
            stage=stage,
            evidence=replace(
                evidence,
                reference=replace(
                    evidence.reference,
                    object_key=object_key,
                ),
            ),
            update_id=f"complete-{stage}",
            expected_revision=0,
        )

        assert result["status"] == "SUCCEEDED"
        assert result["result_reference"]["boundary_id"] == (
            evidence.boundary_id
        )

    ingestion = registry._read(
        f"INGESTION#{admitted.ingestion_id}",
        "META",
    )
    assert ingestion["status"] != "COMPLETED"


@pytest.mark.parametrize(
    "stage",
    ["CDC_DRAINED", "HISTORY_FINALIZED"],
)
def test_downstream_evidence_requires_source_stop(
    shutdown_setup,
    stage,
):
    _, _, _, _, claim, shutdown, evidence = shutdown_setup

    with pytest.raises(ValueError, match="SOURCE_STOPPED"):
        shutdown.record_evidence(
            claim,
            stage=stage,
            evidence=evidence,
            update_id=f"complete-{stage}",
            expected_revision=0,
        )


@pytest.mark.parametrize(
    "stage",
    ["CDC_DRAINED", "HISTORY_FINALIZED"],
)
def test_different_shutdown_boundary_is_rejected(
    shutdown_setup,
    stage,
):
    _, _, _, _, claim, shutdown, evidence = shutdown_setup
    record_source_stopped(shutdown, claim, evidence)

    with pytest.raises(ValueError, match="boundary"):
        shutdown.record_evidence(
            claim,
            stage=stage,
            evidence=replace(
                evidence,
                boundary_id="another-boundary",
            ),
            update_id=f"complete-{stage}",
            expected_revision=0,
        )


def test_evidence_for_another_ingestion_is_rejected(
    shutdown_setup,
):
    _, _, _, _, claim, shutdown, evidence = shutdown_setup

    with pytest.raises(ValueError, match="ingestion_id"):
        record_source_stopped(
            shutdown,
            claim,
            replace(evidence, ingestion_id="another-ingestion"),
        )


def test_evidence_for_another_task_is_rejected(
    shutdown_setup,
):
    _, _, _, _, claim, shutdown, evidence = shutdown_setup

    with pytest.raises(ValueError, match="task_arn"):
        record_source_stopped(
            shutdown,
            claim,
            replace(
                evidence,
                task_arn=(
                    "arn:aws:dms:eu-west-2:"
                    "111122223333:task:ANOTHER"
                ),
            ),
        )


def test_repeated_source_evidence_returns_original_result(
    shutdown_setup,
):
    _, _, _, _, claim, shutdown, evidence = shutdown_setup

    first = record_source_stopped(shutdown, claim, evidence)
    repeated = record_source_stopped(shutdown, claim, evidence)

    assert repeated == first


def test_expired_owner_cannot_record_source_stop(
    shutdown_setup,
):
    _, _, _, clock, claim, shutdown, evidence = shutdown_setup
    clock["now"] = claim.lease_expires_at

    with pytest.raises(OwnershipLost):
        record_source_stopped(shutdown, claim, evidence)


def test_full_load_binding_cannot_record_cdc_shutdown(
    binding_setup,
):
    registry, _, admitted, clock, claim, bindings, binding = (
        binding_setup
    )
    binding["task_group"]["migration_type"] = "full-load"
    bindings.pin(claim, binding)

    shutdown = CdcShutdown(
        IngestionProgress(
            registry,
            clock=lambda: clock["now"],
        )
    )

    evidence = CdcShutdownEvidence(
        ingestion_id=admitted.ingestion_id,
        task_arn=binding["resources"]["replication_task_arn"],
        boundary_id="boundary-one",
        reference=S3ObjectReference(
            bucket_name="example-evidence",
            object_key="shutdown/source-stopped.json",
            version_id="version-one",
            sha256="e" * 64,
        ),
    )

    with pytest.raises(ValueError, match="CDC-enabled"):
        record_source_stopped(shutdown, claim, evidence)