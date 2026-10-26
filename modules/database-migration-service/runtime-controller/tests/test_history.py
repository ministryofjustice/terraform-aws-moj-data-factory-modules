from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from dms_runtime.history import RuntimeOperationHistory
from dms_runtime.operations import (
    RuntimeOperationConflict,
    RuntimeOperationPending,
)
from dms_runtime.ownership import RuntimeScopeDenied

from test_operations import (
    command_result,
    operation_setup,
)
from test_ownership import scope_setup


@pytest.fixture
def history_setup(operation_setup):
    registry, binding, clock, guard, request, operations = (
        operation_setup
    )

    lookup = Mock()
    lookup.registry = registry
    lookup.resolve.return_value = binding

    return {
        "registry": registry,
        "binding": binding,
        "clock": clock,
        "guard": guard,
        "request": request,
        "operations": operations,
        "lookup": lookup,
        "history": RuntimeOperationHistory(lookup),
    }


def record_start(setup, *, outcome="REQUESTED"):
    admission = setup["operations"].begin(
        setup["request"],
        setup["binding"],
    )

    result = {
        **command_result(setup["binding"]),
        "outcome": outcome,
    }

    setup["operations"].finish(
        setup["request"],
        setup["binding"],
        dispatch_token=admission.dispatch_token,
        result=result,
    )

    return admission


def read_start(setup):
    return setup["history"].full_load_start(
        ingestion_id=setup["request"].ingestion_id,
        operation_id=setup["request"].operation_id,
    )


def test_history_returns_original_start_boundary(history_setup):
    setup = history_setup
    record_start(setup)
    setup["clock"]["now"] += 10

    binding, boundary = read_start(setup)

    assert binding == setup["binding"]
    assert boundary == datetime.fromtimestamp(
        1000,
        tz=timezone.utc,
    )

    setup["lookup"].resolve.assert_called_once_with(
        setup["request"].ingestion_id
    )


def test_historical_boundary_does_not_require_active_command_lease(
    history_setup,
):
    setup = history_setup
    record_start(setup)

    setup["clock"]["now"] += 300

    _, boundary = read_start(setup)

    assert boundary == datetime.fromtimestamp(
        1000,
        tz=timezone.utc,
    )


def test_unknown_operation_is_rejected(history_setup):
    with pytest.raises(
        RuntimeOperationConflict,
        match="does not exist",
    ):
        read_start(history_setup)


def test_unresolved_start_has_no_usable_boundary(history_setup):
    setup = history_setup

    setup["operations"].begin(
        setup["request"],
        setup["binding"],
    )

    with pytest.raises(RuntimeOperationPending):
        read_start(setup)


@pytest.mark.parametrize(
    "outcome",
    ["ALREADY_RUNNING", "IN_PROGRESS"],
)
def test_observed_running_state_has_no_usable_boundary(
    history_setup,
    outcome,
):
    setup = history_setup
    record_start(setup, outcome=outcome)

    with pytest.raises(
        RuntimeOperationConflict,
        match="accepted initial start",
    ):
        read_start(setup)


@pytest.mark.parametrize("action", ["resume", "stop"])
def test_resume_or_stop_cannot_supply_start_boundary(
    history_setup,
    action,
):
    setup = history_setup
    request = replace(
        setup["request"],
        action=action,
        operation_id=f"{action}-one",
    )

    admission = setup["operations"].begin(
        request,
        setup["binding"],
    )

    setup["operations"].finish(
        request,
        setup["binding"],
        dispatch_token=admission.dispatch_token,
        result=command_result(setup["binding"], action),
    )

    with pytest.raises(
        RuntimeOperationConflict,
        match="initial start operation",
    ):
        setup["history"].full_load_start(
            ingestion_id=request.ingestion_id,
            operation_id=request.operation_id,
        )


def test_lookup_denial_prevents_operation_read(
    history_setup,
    monkeypatch,
):
    setup = history_setup
    setup["lookup"].resolve.side_effect = RuntimeScopeDenied(
        "Unauthorised service"
    )

    read = Mock()
    monkeypatch.setattr(setup["registry"], "_read", read)

    with pytest.raises(RuntimeScopeDenied):
        read_start(setup)

    read.assert_not_called()


def test_changed_pinned_binding_is_rejected(history_setup):
    setup = history_setup
    record_start(setup)

    setup["lookup"].resolve.return_value = {
        **setup["binding"],
        "artifact_revision": "d" * 64,
    }

    with pytest.raises(
        RuntimeOperationConflict,
        match="pinned binding",
    ):
        read_start(setup)


@pytest.mark.parametrize(
    "boundary",
    [
        "not-a-timestamp",
        "2026-10-26T10:00:00",
    ],
)
def test_invalid_saved_boundary_is_rejected(
    history_setup,
    boundary,
):
    setup = history_setup
    admission = record_start(setup)

    setup["registry"].client.update_item(
        TableName=setup["registry"].table_name,
        Key=setup["registry"]._encode(
            {
                "pk": admission.record["pk"],
                "sk": admission.record["sk"],
            }
        ),
        UpdateExpression="SET full_load_not_before = :boundary",
        ExpressionAttributeValues=setup["registry"]._encode(
            {":boundary": boundary}
        ),
    )

    with pytest.raises(
        RuntimeOperationConflict,
        match="boundary is invalid",
    ):
        read_start(setup)