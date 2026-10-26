from dataclasses import asdict
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from dms_runtime.handler import RuntimeHandler
from dms_runtime.operations import RuntimeOperationPending
from dms_runtime.ownership import RuntimeScopeDenied

from test_history import (
    history_setup,
    record_start,
)
from test_operations import operation_setup
from test_ownership import scope_setup


@pytest.fixture
def handler_setup(history_setup):
    setup = history_setup

    inspection = Mock()
    inspection.task_status.return_value = {
        "status": "running",
    }
    inspection.table_status_page.return_value = {
        "tables": [],
        "next_marker": None,
    }

    readiness = Mock()
    readiness.assess.return_value = {
        "full_load_status": "SUCCEEDED",
        "full_load_ready": True,
    }

    owned = Mock(
        return_value={"outcome": "REQUESTED"}
    )

    handler = RuntimeHandler(
        lookup=setup["lookup"],
        inspection=inspection,
        full_load=readiness,
        owned_action_handler=owned,
    )

    return {
        **setup,
        "inspection": inspection,
        "readiness": readiness,
        "owned": owned,
        "handler": handler,
    }


def read_event(setup, action, **extra):
    return {
        "request_version": 1,
        "action": action,
        "ingestion_id": setup["request"].ingestion_id,
        **extra,
    }


def test_task_status_uses_pinned_lookup(handler_setup):
    setup = handler_setup

    result = setup["handler"].handle(
        read_event(setup, "task_status")
    )

    assert result == {"status": "running"}

    setup["lookup"].resolve.assert_called_once_with(
        setup["request"].ingestion_id
    )
    setup["inspection"].task_status.assert_called_once_with(
        setup["binding"]
    )
    setup["owned"].assert_not_called()


def test_table_status_forwards_cursor(handler_setup):
    setup = handler_setup

    result = setup["handler"].handle(
        read_event(
            setup,
            "table_status",
            marker="next-page",
        )
    )

    assert result["next_marker"] is None

    setup["inspection"].table_status_page.assert_called_once_with(
        setup["binding"],
        marker="next-page",
    )
    setup["owned"].assert_not_called()


@pytest.mark.parametrize(
    "action",
    ["preflight", "start", "resume", "stop"],
)
def test_owned_actions_receive_validated_request_and_pinned_binding(
    handler_setup,
    action,
):
    setup = handler_setup

    event = {
        "request_version": 1,
        "action": action,
        "ingestion_id": setup["request"].ingestion_id,
        "operation_id": f"{action}-one",
        "ingestion_owner": asdict(
            setup["request"].ingestion_owner
        ),
        "task_owner": asdict(
            setup["request"].task_owner
        ),
    }

    result = setup["handler"].handle(event)

    assert result == {"outcome": "REQUESTED"}

    request, binding = setup["owned"].call_args.args

    assert request.action == action
    assert request.operation_id == f"{action}-one"
    assert binding == setup["binding"]

    setup["inspection"].task_status.assert_not_called()
    setup["inspection"].table_status_page.assert_not_called()
    setup["readiness"].assess.assert_not_called()


def test_full_load_uses_saved_boundary_and_selected_tables(
    handler_setup,
):
    setup = handler_setup
    record_start(setup)

    selected = [
        {
            "schema_name": "public",
            "table_name": "example",
        }
    ]

    result = setup["handler"].handle(
        read_event(
            setup,
            "full_load_status",
            start_operation_id="start-one",
            selected_tables=selected,
        )
    )

    assert result == {
        "full_load_status": "SUCCEEDED",
        "full_load_ready": True,
        "start_operation_id": "start-one",
    }

    setup["lookup"].resolve.assert_called_once_with(
        setup["request"].ingestion_id
    )
    setup["readiness"].assess.assert_called_once_with(
        setup["binding"],
        full_load_not_before=datetime.fromtimestamp(
            1000,
            tz=timezone.utc,
        ),
        selected_tables=selected,
    )
    setup["owned"].assert_not_called()


def test_full_load_defaults_to_pinned_task_group_selection(
    handler_setup,
):
    setup = handler_setup
    record_start(setup)

    setup["handler"].handle(
        read_event(
            setup,
            "full_load_status",
            start_operation_id="start-one",
        )
    )

    assert setup["readiness"].assess.call_args.kwargs[
        "selected_tables"
    ] is None


def test_unresolved_start_prevents_full_load_inspection(
    handler_setup,
):
    setup = handler_setup

    setup["operations"].begin(
        setup["request"],
        setup["binding"],
    )

    with pytest.raises(RuntimeOperationPending):
        setup["handler"].handle(
            read_event(
                setup,
                "full_load_status",
                start_operation_id="start-one",
            )
        )

    setup["readiness"].assess.assert_not_called()


def test_invalid_request_is_rejected_before_lookup(
    handler_setup,
):
    setup = handler_setup

    with pytest.raises(ValueError, match="Unsupported request fields"):
        setup["handler"].handle(
            read_event(
                setup,
                "task_status",
                replication_task_arn="untrusted-task",
            )
        )

    setup["lookup"].resolve.assert_not_called()
    setup["owned"].assert_not_called()
    setup["inspection"].task_status.assert_not_called()


def test_lookup_denial_prevents_owned_action(
    handler_setup,
):
    setup = handler_setup
    setup["lookup"].resolve.side_effect = RuntimeScopeDenied(
        "Unauthorised service"
    )

    event = {
        "request_version": 1,
        "action": "start",
        "ingestion_id": setup["request"].ingestion_id,
        "operation_id": "start-one",
        "ingestion_owner": asdict(
            setup["request"].ingestion_owner
        ),
        "task_owner": asdict(
            setup["request"].task_owner
        ),
    }

    with pytest.raises(RuntimeScopeDenied):
        setup["handler"].handle(event)

    setup["owned"].assert_not_called()


def test_owned_action_failures_are_not_hidden(
    handler_setup,
):
    setup = handler_setup
    setup["owned"].side_effect = RuntimeError(
        "Ownership lost"
    )

    event = {
        "request_version": 1,
        "action": "stop",
        "ingestion_id": setup["request"].ingestion_id,
        "operation_id": "stop-one",
        "ingestion_owner": asdict(
            setup["request"].ingestion_owner
        ),
        "task_owner": asdict(
            setup["request"].task_owner
        ),
    }

    with pytest.raises(RuntimeError, match="Ownership lost"):
        setup["handler"].handle(event)