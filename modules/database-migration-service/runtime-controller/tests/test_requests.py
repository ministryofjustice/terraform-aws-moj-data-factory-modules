import copy
from dataclasses import FrozenInstanceError, replace

import pytest

from dms_runtime.requests import RuntimeRequest


@pytest.fixture
def owned_event():
    return {
        "request_version": 1,
        "action": "start",
        "ingestion_id": "ingestion-one",
        "operation_id": "operation-one",
        "ingestion_owner": {
            "execution_id": "table-worker",
            "fencing_token": 1,
            "attempt": 1,
        },
        "task_owner": {
            "execution_id": "group-coordinator",
            "group_fencing_token": 1,
            "task_fencing_token": 1,
        },
    }


@pytest.mark.parametrize(
    "action",
    ["preflight", "start", "resume", "stop"],
)
def test_owned_operations_accept_explicit_identity(
    owned_event,
    action,
):
    owned_event["action"] = action
    result = RuntimeRequest.from_event(owned_event)

    assert result.action == action
    assert result.ingestion_owner.execution_id == "table-worker"
    assert result.task_owner.execution_id == "group-coordinator"


@pytest.mark.parametrize(
    "action",
    ["task_status", "table_status", "full_load_status"],
)
def test_read_requests_need_no_execution_claims(action):
    event = {
        "request_version": 1,
        "action": action,
        "ingestion_id": "ingestion-one",
    }

    if action == "full_load_status":
        event["start_operation_id"] = "original-start"

    assert RuntimeRequest.from_event(event).operation_id is None


@pytest.mark.parametrize(
    "field",
    ["operation_id", "ingestion_owner", "task_owner"],
)
def test_owned_request_requires_complete_identity(
    owned_event,
    field,
):
    del owned_event[field]

    with pytest.raises(ValueError):
        RuntimeRequest.from_event(owned_event)


@pytest.mark.parametrize("token", [0, -1, True, "1"])
def test_invalid_ownership_tokens_are_rejected(
    owned_event,
    token,
):
    owned_event["ingestion_owner"]["fencing_token"] = token

    with pytest.raises(ValueError, match="positive integer"):
        RuntimeRequest.from_event(owned_event)


@pytest.mark.parametrize(
    "field",
    [
        "replication_task_arn",
        "manifest_reference",
        "account_id",
    ],
)
def test_requests_cannot_override_resources_or_context(
    owned_event,
    field,
):
    owned_event[field] = "caller-supplied-value"

    with pytest.raises(
        ValueError,
        match="Unsupported request fields",
    ):
        RuntimeRequest.from_event(owned_event)


def test_boolean_is_not_a_request_version(owned_event):
    owned_event["request_version"] = True

    with pytest.raises(ValueError, match="request_version"):
        RuntimeRequest.from_event(owned_event)


def test_recovery_attempt_preserves_logical_operation_fingerprint(
    owned_event,
):
    first = RuntimeRequest.from_event(owned_event)
    recovered = copy.deepcopy(owned_event)

    recovered["ingestion_owner"].update(
        execution_id="recovery-worker",
        fencing_token=2,
        attempt=2,
    )
    recovered["task_owner"].update(
        execution_id="recovery-coordinator",
        group_fencing_token=2,
        task_fencing_token=2,
    )

    assert (
        RuntimeRequest.from_event(recovered).operation_fingerprint
        == first.operation_fingerprint
    )


def test_same_operation_id_with_another_action_has_a_different_fingerprint(
    owned_event,
):
    first = RuntimeRequest.from_event(owned_event)
    changed = replace(first, action="stop")

    assert changed.operation_fingerprint != first.operation_fingerprint


def test_request_is_immutable_and_detached_from_input(owned_event):
    parsed = RuntimeRequest.from_event(owned_event)
    owned_event["ingestion_owner"]["execution_id"] = "changed"

    assert parsed.ingestion_owner.execution_id == "table-worker"

    with pytest.raises(FrozenInstanceError):
        parsed.action = "stop"


def test_full_load_status_requires_the_original_start_reference():
    with pytest.raises(ValueError, match="start_operation_id"):
        RuntimeRequest.from_event(
            {
                "request_version": 1,
                "action": "full_load_status",
                "ingestion_id": "ingestion-one",
            }
        )


def test_duplicate_selected_tables_are_rejected():
    table = {
        "schema_name": "public",
        "table_name": "example",
    }

    with pytest.raises(ValueError, match="unique"):
        RuntimeRequest.from_event(
            {
                "request_version": 1,
                "action": "full_load_status",
                "ingestion_id": "ingestion-one",
                "start_operation_id": "original-start",
                "selected_tables": [table, table],
            }
        )


def test_table_recovery_reports_unsupported_strategy():
    with pytest.raises(ValueError, match="recovery strategy"):
        RuntimeRequest.from_event(
            {
                "request_version": 1,
                "action": "recover_table",
                "ingestion_id": "ingestion-one",
            }
        )


def test_table_marker_cannot_be_used_for_mutating_operations(
    owned_event,
):
    owned_event["marker"] = "page-two"

    with pytest.raises(ValueError, match="marker"):
        RuntimeRequest.from_event(owned_event)