import pytest

from ingestion_registry.summary import IngestionSummary


def test_unclaimed_ingestion_is_created(owned_ingestion):
    registry, _, admitted, clock = owned_ingestion

    summary = IngestionSummary(
        registry,
        clock=lambda: clock["now"],
    )
    result = summary.read(admitted.ingestion_id)

    assert result["status"] == "CREATED"
    assert result["ownership_active"] is False
    assert result["stages"] == {}


def test_claimed_ingestion_is_running(progress_setup):
    registry, _, admitted, clock, _, _ = progress_setup

    result = IngestionSummary(
        registry,
        clock=lambda: clock["now"],
    ).read(admitted.ingestion_id)

    assert result["status"] == "RUNNING"
    assert result["metadata_status"] == "CREATED"
    assert result["attempt_count"] == 1


@pytest.mark.parametrize(
    "stage_status",
    ["FAILED", "BLOCKED"],
)
def test_summary_exposes_unsuccessful_stages(
    progress_setup,
    stage_status,
):
    registry, _, admitted, clock, claim, progress = progress_setup

    progress.record_stage(
        claim,
        stage="PROCESSING",
        status=stage_status,
        update_id="processing-result",
        expected_revision=0,
        error_summary="Processing needs attention",
    )

    result = IngestionSummary(
        registry,
        clock=lambda: clock["now"],
    ).read(admitted.ingestion_id)

    assert result["status"] == stage_status
    assert result[f"{stage_status.lower()}_stages"] == ["PROCESSING"]
    assert result["ownership_active"] is True


def test_expired_ownership_requires_recovery(progress_setup):
    registry, _, admitted, clock, claim, _ = progress_setup
    clock["now"] = claim.lease_expires_at

    result = IngestionSummary(
        registry,
        clock=lambda: clock["now"],
    ).read(admitted.ingestion_id)

    assert result["status"] == "RECOVERY_REQUIRED"
    assert result["recovery_required"] is True


def test_takeover_exposes_recovery_flag(progress_setup):
    registry, ownership, admitted, clock, claim, _ = progress_setup
    clock["now"] = claim.lease_expires_at

    ownership.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="replacement",
    )

    result = IngestionSummary(
        registry,
        clock=lambda: clock["now"],
    ).read(admitted.ingestion_id)

    assert result["status"] == "RUNNING"
    assert result["recovery_required"] is True
    assert result["attempt_count"] == 2


def test_summary_does_not_write_or_scan(
    progress_setup,
    monkeypatch,
):
    registry, _, admitted, clock, _, _ = progress_setup

    def unexpected(**kwargs):
        pytest.fail(
            "Summary must only read its transaction snapshot"
        )

    for method in (
        "put_item",
        "update_item",
        "scan",
        "get_item",
        "transact_write_items",
    ):
        monkeypatch.setattr(
            registry.client,
            method,
            unexpected,
        )

    result = IngestionSummary(
        registry,
        clock=lambda: clock["now"],
    ).read(admitted.ingestion_id)

    assert result["status"] == "RUNNING"


def test_unknown_ingestion_is_rejected(owned_ingestion):
    registry, _, _, clock = owned_ingestion

    with pytest.raises(ValueError, match="does not exist"):
        IngestionSummary(
            registry,
            clock=lambda: clock["now"],
        ).read("unknown")