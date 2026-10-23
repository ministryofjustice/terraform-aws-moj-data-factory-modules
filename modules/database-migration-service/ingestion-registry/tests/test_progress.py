import pytest
from botocore.exceptions import ReadTimeoutError

from ingestion_registry.ownership import OwnershipLost
from ingestion_registry.progress import ProgressConflict


def test_current_owner_can_record_stage(
    progress_setup,
) -> None:
    _, _, _, _, claim, progress = progress_setup

    result = progress.record_stage(
        claim,
        stage="PROCESSING",
        status="RUNNING",
        update_id="processing-start",
        expected_revision=0,
    )

    assert result["revision"] == 1
    assert result["fencing_token"] == claim.fencing_token
    assert result["status"] == "RUNNING"


def test_repeated_stage_update_returns_original_result(
    progress_setup,
) -> None:
    _, _, _, _, claim, progress = progress_setup

    arguments = {
        "stage": "PROCESSING",
        "status": "RUNNING",
        "update_id": "processing-start",
        "expected_revision": 0,
    }

    first = progress.record_stage(claim, **arguments)
    repeated = progress.record_stage(claim, **arguments)

    assert repeated == first


def test_stage_update_id_cannot_change_content(
    progress_setup,
) -> None:
    _, _, _, _, claim, progress = progress_setup

    progress.record_stage(
        claim,
        stage="PROCESSING",
        status="RUNNING",
        update_id="processing-update",
        expected_revision=0,
    )

    with pytest.raises(ProgressConflict, match="different content"):
        progress.record_stage(
            claim,
            stage="PROCESSING",
            status="FAILED",
            update_id="processing-update",
            expected_revision=0,
            error_summary="Processing failed",
        )


def test_expired_owner_cannot_write_progress(
    progress_setup,
) -> None:
    _, _, admitted, clock, claim, progress = progress_setup
    clock["now"] = claim.lease_expires_at

    with pytest.raises(OwnershipLost):
        progress.record_stage(
            claim,
            stage="PROCESSING",
            status="RUNNING",
            update_id="expired-update",
            expected_revision=0,
        )

    assert progress.read_stage(
        ingestion_id=admitted.ingestion_id,
        stage="PROCESSING",
    ) is None


def test_previous_owner_cannot_write_after_takeover(
    progress_setup,
) -> None:
    _, ownership, admitted, clock, claim, progress = progress_setup
    clock["now"] = claim.lease_expires_at

    recovered = ownership.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="execution-two",
    )

    with pytest.raises(OwnershipLost):
        progress.record_stage(
            claim,
            stage="PROCESSING",
            status="RUNNING",
            update_id="stale-update",
            expected_revision=0,
        )

    result = progress.record_stage(
        recovered,
        stage="PROCESSING",
        status="RUNNING",
        update_id="recovered-update",
        expected_revision=0,
    )

    assert result["fencing_token"] == 2
    assert result["attempt"] == 2


def test_completed_stage_cannot_be_overwritten(
    progress_setup,
) -> None:
    _, _, _, _, claim, progress = progress_setup

    progress.record_stage(
        claim,
        stage="PROCESSING",
        status="SUCCEEDED",
        update_id="processing-complete",
        expected_revision=0,
        result_reference={
            "kind": "component-result",
            "execution_id": "processing-execution-one",
        },
    )

    with pytest.raises(ProgressConflict, match="completed stage"):
        progress.record_stage(
            claim,
            stage="PROCESSING",
            status="RUNNING",
            update_id="processing-restart",
            expected_revision=1,
        )


def test_success_requires_result_evidence(
    progress_setup,
) -> None:
    _, _, _, _, claim, progress = progress_setup

    with pytest.raises(ValueError, match="result_reference"):
        progress.record_stage(
            claim,
            stage="PUBLICATION",
            status="SUCCEEDED",
            update_id="publication-complete",
            expected_revision=0,
        )


def test_history_baseline_and_cdc_progress_are_independent(
    progress_setup,
) -> None:
    _, _, admitted, _, claim, progress = progress_setup

    progress.record_stage(
        claim,
        stage="HISTORY_BASELINE",
        status="SUCCEEDED",
        update_id="baseline-committed",
        expected_revision=0,
        result_reference={
            "kind": "history-manifest",
            "manifest_id": "baseline-one",
        },
    )

    progress.record_stage(
        claim,
        stage="HISTORY_CDC",
        status="FAILED",
        update_id="cdc-history-failed",
        expected_revision=0,
        error_summary="CDC history writer failed",
    )

    baseline = progress.read_stage(
        ingestion_id=admitted.ingestion_id,
        stage="HISTORY_BASELINE",
    )
    cdc = progress.read_stage(
        ingestion_id=admitted.ingestion_id,
        stage="HISTORY_CDC",
    )

    assert baseline["status"] == "SUCCEEDED"
    assert cdc["status"] == "FAILED"


def test_takeover_during_progress_write_blocks_stale_transaction(
    progress_setup,
    monkeypatch,
) -> None:
    registry, ownership, admitted, clock, claim, progress = (
        progress_setup
    )

    original_write = registry.client.transact_write_items
    raced = False

    def takeover_then_write(**kwargs):
        nonlocal raced

        if not raced:
            raced = True
            clock["now"] = claim.lease_expires_at

            ownership.claim(
                ingestion_id=admitted.ingestion_id,
                execution_id="execution-two",
            )

        return original_write(**kwargs)

    monkeypatch.setattr(
        registry.client,
        "transact_write_items",
        takeover_then_write,
    )

    with pytest.raises(OwnershipLost):
        progress.record_stage(
            claim,
            stage="PROCESSING",
            status="RUNNING",
            update_id="stale-in-flight-update",
            expected_revision=0,
        )

    assert progress.read_stage(
        ingestion_id=admitted.ingestion_id,
        stage="PROCESSING",
    ) is None

    updates = registry.client.query(
        TableName=registry.table_name,
        KeyConditionExpression=(
            "pk = :pk AND begins_with(sk, :prefix)"
        ),
        ExpressionAttributeValues={
            ":pk": {
                "S": f"INGESTION#{admitted.ingestion_id}",
            },
            ":prefix": {"S": "STAGEUPDATE#"},
        },
        ConsistentRead=True,
    )

    assert updates["Count"] == 0


def test_lost_progress_response_returns_original_update_on_retry(
    progress_setup,
    monkeypatch,
) -> None:
    registry, _, _, _, claim, progress = progress_setup
    original_write = registry.client.transact_write_items

    def commit_then_timeout(**kwargs):
        original_write(**kwargs)
        raise ReadTimeoutError(
            endpoint_url=registry.client.meta.endpoint_url
        )

    monkeypatch.setattr(
        registry.client,
        "transact_write_items",
        commit_then_timeout,
    )

    arguments = {
        "stage": "PROCESSING",
        "status": "RUNNING",
        "update_id": "processing-start",
        "expected_revision": 0,
    }

    with pytest.raises(ReadTimeoutError):
        progress.record_stage(claim, **arguments)

    committed = progress.read_stage(
        ingestion_id=claim.ingestion_id,
        stage="PROCESSING",
    )
    assert committed["revision"] == 1

    monkeypatch.setattr(
        registry.client,
        "transact_write_items",
        original_write,
    )

    repeated = progress.record_stage(claim, **arguments)

    assert repeated == committed
    assert progress.read_stage(
        ingestion_id=claim.ingestion_id,
        stage="PROCESSING",
    )["revision"] == 1


def test_competing_stage_updates_do_not_overwrite_winner(
    progress_setup,
    monkeypatch,
) -> None:
    registry, _, admitted, _, claim, progress = progress_setup
    original_write = registry.client.transact_write_items
    raced = False
    winner = None

    def competing_write(**kwargs):
        nonlocal raced, winner

        if not raced:
            raced = True
            winner = progress.record_stage(
                claim,
                stage="PROCESSING",
                status="BLOCKED",
                update_id="winning-update",
                expected_revision=0,
                error_summary="Required input is unavailable",
            )

        return original_write(**kwargs)

    monkeypatch.setattr(
        registry.client,
        "transact_write_items",
        competing_write,
    )

    with pytest.raises(ProgressConflict):
        progress.record_stage(
            claim,
            stage="PROCESSING",
            status="RUNNING",
            update_id="competing-update",
            expected_revision=0,
        )

    assert winner is not None
    assert progress.read_stage(
        ingestion_id=admitted.ingestion_id,
        stage="PROCESSING",
    ) == winner


def test_same_update_race_can_be_reconciled_by_retry(
    progress_setup,
    monkeypatch,
) -> None:
    registry, _, _, _, claim, progress = progress_setup
    original_write = registry.client.transact_write_items
    raced = False
    winner = None

    arguments = {
        "stage": "PROCESSING",
        "status": "RUNNING",
        "update_id": "shared-update",
        "expected_revision": 0,
    }

    def competing_write(**kwargs):
        nonlocal raced, winner

        if not raced:
            raced = True
            winner = progress.record_stage(claim, **arguments)

        return original_write(**kwargs)

    monkeypatch.setattr(
        registry.client,
        "transact_write_items",
        competing_write,
    )

    with pytest.raises(ProgressConflict):
        progress.record_stage(claim, **arguments)

    monkeypatch.setattr(
        registry.client,
        "transact_write_items",
        original_write,
    )

    assert progress.record_stage(claim, **arguments) == winner


def test_stale_owner_cannot_acknowledge_duplicate_progress(
    progress_setup,
) -> None:
    _, ownership, admitted, clock, claim, progress = progress_setup

    arguments = {
        "stage": "PROCESSING",
        "status": "RUNNING",
        "update_id": "original-update",
        "expected_revision": 0,
    }

    original = progress.record_stage(claim, **arguments)
    clock["now"] = claim.lease_expires_at

    ownership.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="execution-two",
    )

    with pytest.raises(OwnershipLost):
        progress.record_stage(claim, **arguments)

    assert progress.read_stage(
        ingestion_id=admitted.ingestion_id,
        stage="PROCESSING",
    ) == original


def test_terminal_ingestion_blocks_progress_writes(
    progress_setup,
) -> None:
    registry, _, admitted, _, claim, progress = progress_setup

    registry.client.update_item(
        TableName=registry.table_name,
        Key={
            "pk": {"S": f"INGESTION#{admitted.ingestion_id}"},
            "sk": {"S": "META"},
        },
        UpdateExpression="SET #status = :completed",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":completed": {"S": "COMPLETED"},
        },
    )

    with pytest.raises(OwnershipLost):
        progress.record_stage(
            claim,
            stage="PROCESSING",
            status="RUNNING",
            update_id="late-update",
            expected_revision=0,
        )

    assert progress.read_stage(
        ingestion_id=admitted.ingestion_id,
        stage="PROCESSING",
    ) is None