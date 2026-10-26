from unittest.mock import Mock

import pytest

from dms_runtime.handler import RuntimeHandler
from dms_runtime.ownership import RuntimeScopeDenied
from dms_runtime.requests import RuntimeRequest

from test_history import history_setup
from test_operations import (
    command_result,
    operation_setup,
)
from test_ownership import scope_setup


def status_event(setup, **extra):
    return {
        "request_version": 1,
        "action": "operation_status",
        "ingestion_id": setup["request"].ingestion_id,
        "target_operation_id": setup["request"].operation_id,
        **extra,
    }


def make_handler(setup):
    inspection = Mock()
    inspection.task_status.return_value = {
        "replication_task_arn": setup["binding"][
            "resources"
        ]["replication_task_arn"],
        "status": "running",
    }

    owned = Mock()

    handler = RuntimeHandler(
        lookup=setup["lookup"],
        inspection=inspection,
        full_load=Mock(),
        owned_action_handler=owned,
    )

    return handler, inspection, owned


def test_pending_operation_status_reports_state_without_dispatch(
    history_setup,
    monkeypatch,
):
    setup = history_setup

    setup["operations"].begin(
        setup["request"],
        setup["binding"],
    )

    handler, inspection, owned = make_handler(setup)

    transaction = Mock()
    monkeypatch.setattr(
        setup["registry"].client,
        "transact_write_items",
        transaction,
    )

    result = handler.handle(status_event(setup))

    assert result["reconciliation_status"] == "BLOCKED"
    assert result["reason"] == "COMMAND_OUTCOME_UNKNOWN"
    assert result["task_observation"]["status"] == "running"
    assert result["dispatch_permitted"] is False
    assert "dispatch_token" not in result

    setup["lookup"].resolve.assert_called_once()
    inspection.task_status.assert_called_once()
    owned.assert_not_called()
    transaction.assert_not_called()


def test_recorded_operation_status_does_not_reinspect_task(
    history_setup,
):
    setup = history_setup

    admission = setup["operations"].begin(
        setup["request"],
        setup["binding"],
    )

    setup["operations"].finish(
        setup["request"],
        setup["binding"],
        dispatch_token=admission.dispatch_token,
        result=command_result(setup["binding"]),
    )

    handler, inspection, owned = make_handler(setup)

    result = handler.handle(status_event(setup))

    assert result["reconciliation_status"] == "RESULT_RECORDED"
    assert result["dispatch_permitted"] is False
    inspection.task_status.assert_not_called()
    owned.assert_not_called()


@pytest.mark.parametrize(
    "value",
    [None, "", " original-operation"],
)
def test_operation_status_requires_an_explicit_target(
    history_setup,
    value,
):
    setup = history_setup

    with pytest.raises(ValueError, match="target_operation_id"):
        RuntimeRequest.from_event(
            status_event(
                setup,
                target_operation_id=value,
            )
        )


def test_target_operation_is_rejected_for_other_actions(
    history_setup,
):
    setup = history_setup

    with pytest.raises(
        ValueError,
        match="only supported for operation_status",
    ):
        RuntimeRequest.from_event(
            status_event(
                setup,
                action="task_status",
            )
        )


def test_operation_status_rejects_command_operation_id(
    history_setup,
):
    setup = history_setup

    with pytest.raises(ValueError, match="operation ownership"):
        RuntimeRequest.from_event(
            status_event(
                setup,
                operation_id="new-command",
            )
        )


def test_operation_status_requires_authorised_lookup(
    history_setup,
):
    setup = history_setup
    setup["lookup"].resolve.side_effect = RuntimeScopeDenied(
        "Unauthorised service"
    )

    handler, inspection, owned = make_handler(setup)

    with pytest.raises(RuntimeScopeDenied):
        handler.handle(status_event(setup))

    inspection.task_status.assert_not_called()
    owned.assert_not_called()