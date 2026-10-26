from unittest.mock import Mock

import pytest
from botocore.exceptions import (
    ClientError,
    ReadTimeoutError,
)

from dms_runtime.control import (
    TaskControl,
    TaskOperationBlocked,
)


@pytest.fixture
def control_setup():
    dms = Mock()
    inspection = Mock()
    preflight = Mock()
    guard = Mock(return_value=None)

    binding = {
        "resources": {
            "replication_instance_arn": "instance",
            "source_endpoint_arn": "source",
        }
    }

    task = {
        "replication_task_arn": "task",
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
            "ReplicationTaskArn": "task",
            "Status": "starting",
        }
    }

    dms.stop_replication_task.return_value = {
        "ReplicationTask": {
            "ReplicationTaskArn": "task",
            "Status": "stopping",
        }
    }

    controller = TaskControl(
        dms,
        inspection,
        preflight,
        check_scope=guard,
    )

    return (
        dms,
        inspection,
        preflight,
        guard,
        binding,
        task,
        controller,
    )


def test_start_runs_preflight_and_requests_first_start(
    control_setup,
):
    dms, inspection, preflight, guard, binding, _, controller = (
        control_setup
    )

    result = controller.start(binding)

    dms.start_replication_task.assert_called_once_with(
        ReplicationTaskArn="task",
        StartReplicationTaskType="start-replication",
    )

    preflight.run.assert_called_once()
    assert inspection.task_status.call_count == 2
    assert guard.call_count == 3
    assert result["outcome"] == "REQUESTED"
    assert "full_load_ready" not in result


def test_resume_preserves_checkpoint_without_position_overrides(
    control_setup,
):
    dms, _, _, _, binding, task, controller = control_setup
    task.update(status="stopped", has_started=True)

    assert controller.resume(binding)["outcome"] == "REQUESTED"

    dms.start_replication_task.assert_called_once_with(
        ReplicationTaskArn="task",
        StartReplicationTaskType="resume-processing",
    )


@pytest.mark.parametrize("action", ["start", "resume"])
@pytest.mark.parametrize("status", ["running", "starting"])
def test_repeated_start_or_resume_reconciles_observed_state(
    control_setup,
    action,
    status,
):
    dms, _, preflight, _, binding, task, controller = control_setup
    task["status"] = status

    result = getattr(controller, action)(binding)

    assert result["outcome"] in {
        "ALREADY_RUNNING",
        "IN_PROGRESS",
    }
    dms.start_replication_task.assert_not_called()
    preflight.run.assert_not_called()


@pytest.mark.parametrize("action", ["start", "resume"])
def test_failed_preflight_prevents_start_request(
    control_setup,
    action,
):
    dms, _, preflight, _, binding, task, controller = control_setup

    if action == "resume":
        task.update(status="stopped", has_started=True)

    preflight.run.side_effect = RuntimeError("Source unavailable")

    with pytest.raises(RuntimeError, match="Source unavailable"):
        getattr(controller, action)(binding)

    dms.start_replication_task.assert_not_called()


def test_ownership_lost_during_preflight_prevents_start(
    control_setup,
):
    dms, _, _, guard, binding, _, controller = control_setup

    guard.side_effect = [
        None,
        None,
        RuntimeError("Ownership lost"),
    ]

    with pytest.raises(RuntimeError, match="Ownership lost"):
        controller.start(binding)

    dms.start_replication_task.assert_not_called()


def test_changed_state_after_preflight_prevents_duplicate_start(
    control_setup,
):
    dms, inspection, _, _, binding, task, controller = control_setup

    inspection.task_status.side_effect = [
        task,
        {
            **task,
            "status": "starting",
        },
    ]

    assert controller.start(binding)["outcome"] == "IN_PROGRESS"
    dms.start_replication_task.assert_not_called()


def test_first_start_cannot_be_replaced_with_resume(control_setup):
    dms, _, preflight, _, binding, _, controller = control_setup

    with pytest.raises(TaskOperationBlocked):
        controller.resume(binding)

    dms.start_replication_task.assert_not_called()
    preflight.run.assert_not_called()


def test_completed_or_stopped_task_is_not_started_again(
    control_setup,
):
    dms, _, _, _, binding, task, controller = control_setup
    task.update(status="stopped", has_started=True)

    with pytest.raises(TaskOperationBlocked):
        controller.start(binding)

    dms.start_replication_task.assert_not_called()


def test_full_load_resume_is_rejected_because_it_can_reload_tables(
    control_setup,
):
    dms, _, _, _, binding, task, controller = control_setup

    task.update(
        status="stopped",
        has_started=True,
        migration_type="full-load",
    )

    with pytest.raises(
        TaskOperationBlocked,
        match="reload partial tables",
    ):
        controller.resume(binding)

    dms.start_replication_task.assert_not_called()


def test_stop_requests_shutdown_without_claiming_drain(
    control_setup,
):
    dms, _, preflight, _, binding, task, controller = control_setup
    task["status"] = "running"

    result = controller.stop(binding)

    dms.stop_replication_task.assert_called_once_with(
        ReplicationTaskArn="task",
    )
    preflight.run.assert_not_called()

    assert result["outcome"] == "REQUESTED"
    assert result["task_status"] == "stopping"
    assert "drained" not in result


@pytest.mark.parametrize(
    "status",
    ["stopped", "stopping", "starting", "ready"],
)
def test_repeated_or_pending_stop_does_not_issue_another_request(
    control_setup,
    status,
):
    dms, _, _, _, binding, task, controller = control_setup
    task["status"] = status

    controller.stop(binding)

    dms.stop_replication_task.assert_not_called()


def test_invalid_state_race_is_reconciled_without_reissuing_start(
    control_setup,
):
    dms, inspection, _, _, binding, task, controller = control_setup

    inspection.task_status.side_effect = [
        task,
        task,
        {
            **task,
            "status": "starting",
        },
    ]

    dms.start_replication_task.side_effect = ClientError(
        {"Error": {"Code": "InvalidResourceStateFault"}},
        "StartReplicationTask",
    )

    assert controller.start(binding)["outcome"] == "IN_PROGRESS"
    assert dms.start_replication_task.call_count == 1


def test_permission_failure_is_not_hidden_by_reconciliation(
    control_setup,
):
    dms, inspection, _, _, binding, _, controller = control_setup

    dms.start_replication_task.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedFault"}},
        "StartReplicationTask",
    )

    with pytest.raises(ClientError):
        controller.start(binding)

    assert inspection.task_status.call_count == 2


def test_retry_after_lost_start_response_checks_actual_state(
    control_setup,
):
    dms, _, _, _, binding, task, controller = control_setup

    def start_then_timeout(**kwargs):
        task["status"] = "starting"

        raise ReadTimeoutError(
            endpoint_url="https://dms.eu-west-2.amazonaws.com",
        )

    dms.start_replication_task.side_effect = start_then_timeout

    with pytest.raises(ReadTimeoutError):
        controller.start(binding)

    assert controller.start(binding)["outcome"] == "IN_PROGRESS"
    assert dms.start_replication_task.call_count == 1


def test_postgres_credential_sync_reference_is_forwarded(
    control_setup,
):
    dms, inspection, preflight, guard, binding, _, _ = control_setup

    controller = TaskControl(
        dms,
        inspection,
        preflight,
        check_scope=guard,
        credential_sync_function_arn="authorised-sync-function",
    )

    controller.start(binding)

    assert preflight.run.call_args.kwargs[
        "credential_sync_function_arn"
    ] == "authorised-sync-function"


def test_false_guard_result_cannot_silently_authorise_work(
    control_setup,
):
    dms, inspection, _, guard, binding, _, controller = control_setup
    guard.return_value = False

    with pytest.raises(ValueError, match="check_scope"):
        controller.start(binding)

    inspection.task_status.assert_not_called()
    dms.start_replication_task.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("full_load_progress_percent", 50),
        ("tables_loading", 1),
        ("tables_queued", 1),
        ("tables_errored", 1),
        ("tables_loading", None),
    ],
)
def test_resume_does_not_attempt_partial_full_load_recovery(
    control_setup,
    field,
    value,
):
    dms, _, preflight, _, binding, task, controller = control_setup
    task.update(status="stopped", has_started=True)
    task[field] = value

    with pytest.raises(TaskOperationBlocked):
        controller.resume(binding)

    preflight.run.assert_not_called()
    dms.start_replication_task.assert_not_called()