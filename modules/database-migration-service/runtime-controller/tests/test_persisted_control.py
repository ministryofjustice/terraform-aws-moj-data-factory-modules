from dataclasses import replace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ReadTimeoutError

from dms_runtime.control import TaskControl
from dms_runtime.operations import RuntimeOperationPending
from dms_runtime.ownership import RuntimeOwnershipLost

from test_operations import operation_setup
from test_ownership import scope_setup


@pytest.fixture
def persisted_setup(operation_setup):
    registry, binding, clock, guard, request, operations = (
        operation_setup
    )

    dms = Mock()
    dms.meta.config.retries = {
        "total_max_attempts": 1,
        "mode": "standard",
    }

    inspection = Mock()
    preflight = Mock()

    task = {
        "replication_task_arn": binding[
            "resources"
        ]["replication_task_arn"],
        "status": "ready",
        "has_started": False,
        "migration_type": "full-load-and-cdc",
        "full_load_progress_percent": 100,
        "tables_loading": 0,
        "tables_queued": 0,
        "tables_errored": 0,
    }

    inspection.task_status.return_value = task
    preflight.run.return_value = {
        "connectionStatus": "successful",
    }

    dms.start_replication_task.return_value = {
        "ReplicationTask": {
            "ReplicationTaskArn": task["replication_task_arn"],
            "Status": "starting",
        }
    }

    dms.stop_replication_task.return_value = {
        "ReplicationTask": {
            "ReplicationTaskArn": task["replication_task_arn"],
            "Status": "stopping",
        }
    }

    controller = TaskControl(
        dms,
        inspection,
        preflight,
        check_scope=guard,
        operations=operations,
        credential_sync_function_arn="authorised-sync-function",
    )

    return {
        "registry": registry,
        "binding": binding,
        "clock": clock,
        "guard": guard,
        "request": request,
        "operations": operations,
        "dms": dms,
        "inspection": inspection,
        "preflight": preflight,
        "task": task,
        "controller": controller,
    }


def test_start_persists_result_and_boundary_after_preflight(
    persisted_setup,
):
    setup = persisted_setup

    def successful_preflight(**kwargs):
        setup["clock"]["now"] += 10
        return {"connectionStatus": "successful"}

    setup["preflight"].run.side_effect = successful_preflight

    result = setup["controller"].start(
        setup["binding"],
        request=setup["request"],
    )

    assert result["outcome"] == "REQUESTED"
    assert result["operation_id"] == "start-one"
    assert result["full_load_not_before"] == (
        "1970-01-01T00:16:50+00:00"
    )
    assert "full_load_ready" not in result

    setup["dms"].start_replication_task.assert_called_once_with(
        ReplicationTaskArn=setup["task"]["replication_task_arn"],
        StartReplicationTaskType="start-replication",
    )

    assert setup["preflight"].run.call_args.kwargs[
        "credential_sync_function_arn"
    ] == "authorised-sync-function"


def test_delayed_start_retry_after_stop_returns_original_result(
    persisted_setup,
):
    setup = persisted_setup
    controller = setup["controller"]
    binding = setup["binding"]
    request = setup["request"]

    original = controller.start(binding, request=request)

    setup["task"].update(
        status="running",
        has_started=True,
    )

    controller.stop(
        binding,
        request=replace(
            request,
            action="stop",
            operation_id="stop-one",
        ),
    )

    setup["task"]["status"] = "stopped"
    setup["clock"]["now"] += 10
    setup["inspection"].task_status.reset_mock()
    setup["preflight"].run.reset_mock()

    retry = controller.start(binding, request=request)

    assert retry == original
    assert setup["dms"].start_replication_task.call_count == 1
    setup["inspection"].task_status.assert_not_called()
    setup["preflight"].run.assert_not_called()


def test_delayed_stop_retry_after_resume_does_not_stop_again(
    persisted_setup,
):
    setup = persisted_setup
    controller = setup["controller"]
    binding = setup["binding"]
    request = setup["request"]

    setup["task"].update(
        status="running",
        has_started=True,
    )

    stop_request = replace(
        request,
        action="stop",
        operation_id="stop-one",
    )

    original = controller.stop(
        binding,
        request=stop_request,
    )

    setup["task"]["status"] = "stopped"

    controller.resume(
        binding,
        request=replace(
            request,
            action="resume",
            operation_id="resume-one",
        ),
    )

    setup["task"]["status"] = "running"
    setup["inspection"].task_status.reset_mock()

    retry = controller.stop(
        binding,
        request=stop_request,
    )

    assert retry == original
    assert setup["dms"].stop_replication_task.call_count == 1
    setup["inspection"].task_status.assert_not_called()

    setup["dms"].start_replication_task.assert_called_once_with(
        ReplicationTaskArn=setup["task"]["replication_task_arn"],
        StartReplicationTaskType="resume-processing",
    )


def test_failed_preflight_can_retry_without_pending_reservation(
    persisted_setup,
):
    setup = persisted_setup
    setup["preflight"].run.side_effect = RuntimeError(
        "Source unavailable"
    )

    with pytest.raises(RuntimeError, match="Source unavailable"):
        setup["controller"].start(
            setup["binding"],
            request=setup["request"],
        )

    assert setup["registry"]._read(
        setup["guard"].task_claim.task_key,
        "RUNTIME_COMMAND",
    ) is None

    setup["dms"].start_replication_task.assert_not_called()

    setup["preflight"].run.side_effect = None

    result = setup["controller"].start(
        setup["binding"],
        request=setup["request"],
    )

    assert result["outcome"] == "REQUESTED"
    setup["dms"].start_replication_task.assert_called_once()


@pytest.mark.parametrize("action", ["start", "resume", "stop"])
def test_lost_dms_response_blocks_retry_without_another_command(
    persisted_setup,
    action,
):
    setup = persisted_setup

    request = replace(
        setup["request"],
        action=action,
        operation_id=f"{action}-one",
    )

    if action == "resume":
        setup["task"].update(
            status="stopped",
            has_started=True,
        )
    elif action == "stop":
        setup["task"].update(
            status="running",
            has_started=True,
        )

    command = (
        setup["dms"].stop_replication_task
        if action == "stop"
        else setup["dms"].start_replication_task
    )

    command.side_effect = ReadTimeoutError(
        endpoint_url="https://dms.eu-west-2.amazonaws.com"
    )

    method = getattr(setup["controller"], action)

    with pytest.raises(ReadTimeoutError):
        method(setup["binding"], request=request)

    setup["task"]["status"] = (
        "stopped" if action == "stop" else "running"
    )
    setup["inspection"].task_status.reset_mock()

    with pytest.raises(RuntimeOperationPending):
        method(setup["binding"], request=request)

    assert command.call_count == 1
    setup["inspection"].task_status.assert_not_called()


def test_lost_result_write_does_not_repeat_accepted_command(
    persisted_setup,
    monkeypatch,
):
    setup = persisted_setup

    monkeypatch.setattr(
        setup["operations"],
        "finish",
        Mock(
            side_effect=ReadTimeoutError(
                endpoint_url=(
                    "https://dynamodb.eu-west-2.amazonaws.com"
                )
            )
        ),
    )

    with pytest.raises(ReadTimeoutError):
        setup["controller"].start(
            setup["binding"],
            request=setup["request"],
        )

    with pytest.raises(RuntimeOperationPending):
        setup["controller"].start(
            setup["binding"],
            request=setup["request"],
        )

    setup["dms"].start_replication_task.assert_called_once()


def test_already_running_observation_has_no_start_boundary(
    persisted_setup,
):
    setup = persisted_setup
    setup["task"].update(
        status="running",
        has_started=True,
    )

    result = setup["controller"].start(
        setup["binding"],
        request=setup["request"],
    )

    assert result["outcome"] == "ALREADY_RUNNING"
    assert result["operation_id"] == "start-one"
    assert "full_load_not_before" not in result

    setup["dms"].start_replication_task.assert_not_called()
    setup["preflight"].run.assert_not_called()


def test_ownership_lost_after_reservation_prevents_dispatch(
    persisted_setup,
    monkeypatch,
):
    setup = persisted_setup
    original_begin = setup["operations"].begin

    def reserve_then_expire(request, binding):
        admission = original_begin(request, binding)
        setup["clock"]["now"] += 300
        return admission

    monkeypatch.setattr(
        setup["operations"],
        "begin",
        reserve_then_expire,
    )

    with pytest.raises(RuntimeOwnershipLost):
        setup["controller"].start(
            setup["binding"],
            request=setup["request"],
        )

    setup["dms"].start_replication_task.assert_not_called()

    assert setup["registry"]._read(
        setup["guard"].task_claim.task_key,
        "RUNTIME_COMMAND",
    ) is not None


def test_persisted_control_requires_a_matching_request(
    persisted_setup,
):
    setup = persisted_setup

    with pytest.raises(ValueError, match="matching RuntimeRequest"):
        setup["controller"].start(setup["binding"])

    with pytest.raises(ValueError, match="matching RuntimeRequest"):
        setup["controller"].start(
            setup["binding"],
            request=replace(
                setup["request"],
                action="stop",
            ),
        )

    setup["inspection"].task_status.assert_not_called()
    setup["dms"].start_replication_task.assert_not_called()


@pytest.mark.parametrize(
    "retries",
    [
        {},
        {"max_attempts": 0},
        {"total_max_attempts": 2},
    ],
)
def test_persisted_control_rejects_automatic_command_retries(
    persisted_setup,
    retries,
):
    setup = persisted_setup
    setup["dms"].meta.config.retries = retries

    with pytest.raises(ValueError, match="total_max_attempts=1"):
        TaskControl(
            setup["dms"],
            setup["inspection"],
            setup["preflight"],
            check_scope=setup["guard"],
            operations=setup["operations"],
        )