import pytest

from ingestion_registry.admission import IngestionConflict
from ingestion_registry.lifecycle import (
    BASELINE_STAGES,
    IngestionLifecycle,
    LifecycleConflict,
)
from ingestion_registry.ownership import OwnershipLost
from ingestion_registry.requests import NewIngestionRequest


@pytest.fixture
def lifecycle_setup(progress_setup):
    (
        registry,
        ownership,
        admitted,
        clock,
        claim,
        progress,
    ) = progress_setup

    lifecycle = IngestionLifecycle(
        progress,
        clock=lambda: clock["now"],
    )

    return (
        registry,
        ownership,
        admitted,
        clock,
        claim,
        progress,
        lifecycle,
    )


def succeed_stages(progress, claim, stages):
    for stage in stages:
        progress.record_stage(
            claim,
            stage=stage,
            status="SUCCEEDED",
            update_id=f"complete-{stage}",
            expected_revision=0,
            result_reference={
                "uri": f"s3://example-evidence/{stage}.json",
                "version_id": "evidence-version-one",
            },
        )


def test_baseline_records_required_stage_evidence(
    lifecycle_setup,
):
    _, _, _, _, claim, progress, lifecycle = lifecycle_setup
    succeed_stages(progress, claim, BASELINE_STAGES)

    result = lifecycle.mark_baseline_ready(
        claim,
        reconciliation_required=False,
    )

    assert result["status"] == "READY"
    assert result["ingestion_id"] == claim.ingestion_id
    assert set(result["stage_results"]) == set(BASELINE_STAGES)
    assert result["stage_results"]["HISTORY_BASELINE"]["revision"] == 1


@pytest.mark.parametrize("missing_stage", BASELINE_STAGES)
def test_missing_stage_blocks_baseline(
    lifecycle_setup,
    missing_stage,
):
    _, _, _, _, claim, progress, lifecycle = lifecycle_setup

    succeed_stages(
        progress,
        claim,
        [
            stage
            for stage in BASELINE_STAGES
            if stage != missing_stage
        ],
    )

    with pytest.raises(LifecycleConflict, match=missing_stage):
        lifecycle.mark_baseline_ready(
            claim,
            reconciliation_required=False,
        )


def test_required_reconciliation_must_succeed(
    lifecycle_setup,
):
    _, _, _, _, claim, progress, lifecycle = lifecycle_setup
    succeed_stages(progress, claim, BASELINE_STAGES)

    with pytest.raises(LifecycleConflict, match="RECONCILIATION"):
        lifecycle.mark_baseline_ready(
            claim,
            reconciliation_required=True,
        )

    succeed_stages(progress, claim, ["RECONCILIATION"])

    result = lifecycle.mark_baseline_ready(
        claim,
        reconciliation_required=True,
    )

    assert "RECONCILIATION" in result["stage_results"]


def test_repeated_baseline_returns_original_result(
    lifecycle_setup,
):
    _, _, _, clock, claim, progress, lifecycle = lifecycle_setup
    succeed_stages(progress, claim, BASELINE_STAGES)

    first = lifecycle.mark_baseline_ready(
        claim,
        reconciliation_required=False,
    )

    clock["now"] += 10

    repeated = lifecycle.mark_baseline_ready(
        claim,
        reconciliation_required=False,
    )

    assert repeated == first


def test_baseline_requirement_cannot_be_changed(
    lifecycle_setup,
):
    _, _, _, _, claim, progress, lifecycle = lifecycle_setup
    succeed_stages(progress, claim, BASELINE_STAGES)

    lifecycle.mark_baseline_ready(
        claim,
        reconciliation_required=False,
    )

    with pytest.raises(LifecycleConflict, match="different"):
        lifecycle.mark_baseline_ready(
            claim,
            reconciliation_required=True,
        )


def test_baseline_keeps_table_admission_active(
    lifecycle_setup,
):
    registry, _, admitted, _, claim, progress, lifecycle = (
        lifecycle_setup
    )
    succeed_stages(progress, claim, BASELINE_STAGES)

    lifecycle.mark_baseline_ready(
        claim,
        reconciliation_required=False,
    )

    ingestion = registry._read(
        f"INGESTION#{admitted.ingestion_id}",
        "META",
    )

    from ingestion_registry.identity import TableIdentity

    table = TableIdentity(**ingestion["table_identity"])
    head = registry._read(table.partition_key, "HEAD")

    assert head["active_ingestion_id"] == admitted.ingestion_id
    assert int(head["latest_version"]) == 1

    request = NewIngestionRequest(
        request_id="reingestion-after-baseline",
        operation="REINGESTION",
        table=table,
        configuration_revision=ingestion["configuration_revision"],
        artifact_revision=ingestion["artifact_revision"],
        extraction_generation="generation-two",
    )

    with pytest.raises(IngestionConflict, match="active ingestion"):
        registry.admit(request)


def test_cdc_progress_can_continue_after_baseline(
    lifecycle_setup,
):
    _, _, _, _, claim, progress, lifecycle = lifecycle_setup
    succeed_stages(progress, claim, BASELINE_STAGES)

    lifecycle.mark_baseline_ready(
        claim,
        reconciliation_required=False,
    )

    result = progress.record_stage(
        claim,
        stage="HISTORY_CDC",
        status="RUNNING",
        update_id="history-cdc-running",
        expected_revision=0,
    )

    assert result["status"] == "RUNNING"


def test_stale_owner_cannot_record_baseline(
    lifecycle_setup,
):
    _, ownership, admitted, clock, claim, progress, lifecycle = (
        lifecycle_setup
    )
    succeed_stages(progress, claim, BASELINE_STAGES)

    clock["now"] = claim.lease_expires_at

    ownership.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="execution-two",
    )

    with pytest.raises(OwnershipLost):
        lifecycle.mark_baseline_ready(
            claim,
            reconciliation_required=False,
        )


def test_expired_owner_cannot_repeat_baseline(
    lifecycle_setup,
):
    _, _, _, clock, claim, progress, lifecycle = lifecycle_setup
    succeed_stages(progress, claim, BASELINE_STAGES)

    lifecycle.mark_baseline_ready(
        claim,
        reconciliation_required=False,
    )

    clock["now"] = claim.lease_expires_at

    with pytest.raises(OwnershipLost):
        lifecycle.mark_baseline_ready(
            claim,
            reconciliation_required=False,
        )


def test_returned_baseline_is_detached(
    lifecycle_setup,
):
    _, _, _, _, claim, progress, lifecycle = lifecycle_setup
    succeed_stages(progress, claim, BASELINE_STAGES)

    first = lifecycle.mark_baseline_ready(
        claim,
        reconciliation_required=False,
    )
    first["stage_results"].clear()

    repeated = lifecycle.mark_baseline_ready(
        claim,
        reconciliation_required=False,
    )

    assert set(repeated["stage_results"]) == set(BASELINE_STAGES)


@pytest.mark.parametrize(
    "value",
    [None, 0, 1, "false"],
)
def test_reconciliation_requirement_must_be_boolean(
    lifecycle_setup,
    value,
):
    _, _, _, _, claim, _, lifecycle = lifecycle_setup

    with pytest.raises(ValueError, match="boolean"):
        lifecycle.mark_baseline_ready(
            claim,
            reconciliation_required=value,
        )