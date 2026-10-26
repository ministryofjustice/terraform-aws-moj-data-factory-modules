from dataclasses import replace
from unittest.mock import Mock

import pytest

from dms_runtime.ownership import RuntimeScopeDenied
from dms_runtime.reconciliation import RuntimeReconciliation

from test_history import history_setup
from test_operations import (
    command_result,
    operation_setup,
)
from test_ownership import scope_setup


@pytest.mark.parametrize("action", ["start", "resume", "stop"])
def test_task_state_does_not_resolve_an_uncertain_command(
    history_setup,
    action,
    monkeypatch,
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

    inspection = Mock()
    inspection.task_status.return_value = {
        "replication_task_arn": setup["binding"][
            "resources"
        ]["replication_task_arn"],
        "status": (
            "stopped" if action == "stop" else "running"
        ),
    }

    transaction = Mock()
    monkeypatch.setattr(
        setup["registry"].client,
        "transact_write_items",
        transaction,
    )

    report = RuntimeReconciliation(
        history=setup["history"],
        inspection=inspection,
    ).inspect(
        ingestion_id=request.ingestion_id,
        operation_id=request.operation_id,
    )

    assert report["reconciliation_status"] == "BLOCKED"
    assert report["reason"] == "COMMAND_OUTCOME_UNKNOWN"
    assert report["command_gate"] == "HELD"
    assert report["dispatch_permitted"] is False
    assert report["automatic_resolution_available"] is False
    assert "full_load_ready" not in report
    assert "drained" not in report
    assert "dispatch_token" not in report

    assert report["operation_reference"] == {
        "table_name": setup["registry"].table_name,
        "partition_key": admission.record["pk"],
        "sort_key": admission.record["sk"],
    }

    inspection.task_status.assert_called_once_with(
        setup["binding"]
    )
    transaction.assert_not_called()

    assert setup["registry"]._read(
        admission.record["pk"],
        admission.record["sk"],
    )["status"] == "DISPATCHING"


def test_recorded_result_needs_no_current_task_observation(
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

    inspection = Mock()

    report = RuntimeReconciliation(
        history=setup["history"],
        inspection=inspection,
    ).inspect(
        ingestion_id=setup["request"].ingestion_id,
        operation_id=setup["request"].operation_id,
    )

    assert report["reconciliation_status"] == "RESULT_RECORDED"
    assert report["dispatch_permitted"] is False
    inspection.task_status.assert_not_called()


def test_missing_gate_does_not_authorise_dispatch(history_setup):
    setup = history_setup

    setup["operations"].begin(
        setup["request"],
        setup["binding"],
    )

    setup["registry"].client.delete_item(
        TableName=setup["registry"].table_name,
        Key=setup["registry"]._encode(
            {
                "pk": setup["guard"].task_claim.task_key,
                "sk": "RUNTIME_COMMAND",
            }
        ),
    )

    report = RuntimeReconciliation(
        history=setup["history"],
        inspection=Mock(),
    ).inspect(
        ingestion_id=setup["request"].ingestion_id,
        operation_id=setup["request"].operation_id,
    )

    assert report["reconciliation_status"] == "BLOCKED"
    assert report["command_gate"] == "MISSING_OR_CHANGED"
    assert report["dispatch_permitted"] is False


def test_unauthorised_lookup_prevents_task_inspection(
    history_setup,
):
    setup = history_setup
    setup["lookup"].resolve.side_effect = RuntimeScopeDenied(
        "Unauthorised service"
    )

    inspection = Mock()

    with pytest.raises(RuntimeScopeDenied):
        RuntimeReconciliation(
            history=setup["history"],
            inspection=inspection,
        ).inspect(
            ingestion_id=setup["request"].ingestion_id,
            operation_id=setup["request"].operation_id,
        )

    inspection.task_status.assert_not_called()