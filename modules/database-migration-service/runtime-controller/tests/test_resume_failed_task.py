import pytest

from dms_runtime.control import TaskControl, TaskOperationBlocked


def completed_task(**changes):
    return {
        "status": "failed",
        "has_started": True,
        "migration_type": "full-load-and-cdc",
        "full_load_progress_percent": 100,
        "tables_loading": 0,
        "tables_queued": 0,
        "tables_errored": 0,
        **changes,
    }


@pytest.mark.parametrize("status", ["stopped", "failed"])
def test_completed_full_load_can_resume(status):
    TaskControl._require_start_state(
        completed_task(status=status),
        "resume",
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"full_load_progress_percent": 99},
        {"tables_loading": 1},
        {"tables_queued": 1},
        {"tables_errored": 1},
        {"has_started": False},
    ],
)
def test_failed_task_must_pass_existing_resume_checks(changes):
    with pytest.raises(TaskOperationBlocked):
        TaskControl._require_start_state(
            completed_task(**changes),
            "resume",
        )


@pytest.mark.parametrize(
    "status",
    ["ready", "starting", "running", "stopping"],
)
def test_other_states_do_not_pass_resume_state_check(status):
    with pytest.raises(TaskOperationBlocked):
        TaskControl._require_start_state(
            completed_task(status=status),
            "resume",
        )