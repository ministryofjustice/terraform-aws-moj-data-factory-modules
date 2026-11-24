from dataclasses import replace
from unittest.mock import Mock

import pytest

from dms_runtime.control import TaskOperationBlocked
from dms_runtime.ownership import RuntimeOwnershipLost
from dms_runtime.raw_batch_actions import (
    RawBatchAction,
    TaskGuardedProgress,
)
from dms_runtime.requests import RuntimeRequest, SelectedTable
from ingestion_registry.references import S3ObjectReference

from test_owned_actions import make_adapter
from test_persisted_control import persisted_setup
from test_operations import operation_setup
from test_ownership import scope_setup


def request_for(setup):
    return replace(
        setup["request"],
        action="commit_raw_batch",
        operation_id="raw-batch-one",
        start_operation_id="start-one",
    )


def test_raw_batch_requires_recorded_start(persisted_setup):
    with pytest.raises(ValueError, match="start_operation_id"):
        replace(
            persisted_setup["request"],
            action="commit_raw_batch",
        )


def test_raw_batch_requires_ownership():
    with pytest.raises(
        ValueError,
        match="ownership|ingestion_owner",
    ):
        RuntimeRequest.from_event({
            "request_version": 1,
            "action": "commit_raw_batch",
            "ingestion_id": "one",
            "operation_id": "batch-one",
            "start_operation_id": "start-one",
        })


def test_raw_batch_fingerprint_pins_start(persisted_setup):
    request = request_for(persisted_setup)

    another = replace(
        request,
        start_operation_id="another-start",
    )

    assert request.operation_fingerprint != another.operation_fingerprint


def test_raw_batch_rejects_table_override(persisted_setup):
    with pytest.raises(ValueError, match="selected_tables"):
        replace(
            request_for(persisted_setup),
            selected_tables=(
                SelectedTable("public", "another"),
            ),
        )


def test_raw_batch_is_disabled_by_default(persisted_setup):
    with pytest.raises(
        TaskOperationBlocked,
        match="not configured",
    ):
        make_adapter(persisted_setup)(
            request_for(persisted_setup),
            persisted_setup["binding"],
        )


def test_raw_batch_dispatches_after_scope_check(persisted_setup):
    adapter = make_adapter(persisted_setup)
    action = Mock(return_value={"outcome": "SUCCEEDED"})
    adapter.raw_batch_action = action

    request = request_for(persisted_setup)

    result = adapter(
        request,
        persisted_setup["binding"],
    )

    assert result == {"outcome": "SUCCEEDED"}
    action.assert_called_once()

    arguments = action.call_args.args

    assert arguments[:2] == (
        request,
        persisted_setup["binding"],
    )
    assert (
        arguments[2].ingestion_claim.ingestion_id
        == request.ingestion_id
    )

    persisted_setup["dms"].start_replication_task.assert_not_called()


def test_progress_uses_complete_scope_transaction():
    guard = Mock()
    guard.ingestion_claim = object()
    guard.transaction_checks.return_value = ["scope-checks"]

    binding = {"example": "pinned"}

    progress = TaskGuardedProgress(
        guard,
        binding,
    )

    assert progress._guards(
        guard.ingestion_claim,
        1000,
    ) == ["scope-checks"]

    guard.transaction_checks.assert_called_once_with(
        binding,
        "commit_raw_batch",
    )

    with pytest.raises(ValueError, match="claim"):
        progress._guards(object(), 1000)


def test_raw_batch_action_returns_manifest(
    persisted_setup,
    monkeypatch,
):
    import dms_runtime.raw_batch_actions as module

    reference = S3ObjectReference(
        bucket_name="manifest-bucket",
        object_key="batches/manifest.json",
        version_id="version-one",
        sha256="a" * 64,
    )

    handoff = Mock()
    handoff.advance.return_value = reference

    factory = Mock(return_value=handoff)

    monkeypatch.setattr(
        module,
        "FullLoadRawBatchHandoff",
        factory,
    )

    adapter = make_adapter(persisted_setup)

    adapter.raw_batch_action = RawBatchAction(
        client=Mock(),
        history=Mock(),
        readiness=Mock(),
        manifest_location={
            "bucket_name": "manifest-bucket",
            "prefix": "batches/",
            "expected_bucket_owner": "111122223333",
        },
        allowed_landings=[
            persisted_setup["binding"]["landing"],
        ],
    )

    result = adapter(
        request_for(persisted_setup),
        persisted_setup["binding"],
    )

    assert result["outcome"] == "SUCCEEDED"
    assert (
        result["raw_batch_manifest"]["version_id"]
        == "version-one"
    )

    producer = factory.call_args.kwargs["producer"]

    assert isinstance(
        producer._progress,
        TaskGuardedProgress,
    )

    assert handoff.advance.call_args.kwargs == {
        "start_operation_id": "start-one",
    }


def test_raw_batch_action_rejects_unconfigured_landing(
    persisted_setup,
):
    adapter = make_adapter(persisted_setup)

    adapter.raw_batch_action = RawBatchAction(
        client=Mock(),
        history=Mock(),
        readiness=Mock(),
        manifest_location={},
        allowed_landings=[],
    )

    with pytest.raises(PermissionError, match="landing"):
        adapter(
            request_for(persisted_setup),
            persisted_setup["binding"],
        )


def test_expired_owner_cannot_dispatch_raw_batch(
    persisted_setup,
):
    adapter = make_adapter(persisted_setup)
    action = Mock()
    adapter.raw_batch_action = action

    persisted_setup["clock"]["now"] += 300

    with pytest.raises(RuntimeOwnershipLost):
        adapter(
            request_for(persisted_setup),
            persisted_setup["binding"],
        )

    action.assert_not_called()