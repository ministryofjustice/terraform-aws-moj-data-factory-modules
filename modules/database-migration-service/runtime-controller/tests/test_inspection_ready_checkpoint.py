from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from dms_runtime.inspection import TaskInspection


@pytest.mark.parametrize(
    "change,expected",
    [
        ({}, False),
        ({"Status": "stopped"}, True),
        ({"CdcStartPosition": "checkpoint:configured"}, True),
        ({"ReplicationTaskStartDate": datetime.now(timezone.utc)}, True),
        ({"stats": {"StartDate": datetime.now(timezone.utc)}}, True),
        ({"stats": {"ElapsedTimeMillis": 1}}, True),
        ({"stats": {"TablesLoaded": 1}}, True),
        ({"missing_counter": "TablesErrored"}, True),
    ],
)
def test_ready_checkpoint_requires_complete_zero_activity(change, expected):
    resources = {
        "replication_task_arn": "task",
        "replication_instance_arn": "instance",
        "source_endpoint_arn": "source",
        "target_endpoint_arn": "target",
    }
    binding = {
        "resources": resources,
        "task_group": {"migration_type": "full-load-and-cdc"},
    }
    task = {
        "ReplicationTaskArn": "task",
        "ReplicationInstanceArn": "instance",
        "SourceEndpointArn": "source",
        "TargetEndpointArn": "target",
        "MigrationType": "full-load-and-cdc",
        "Status": "ready",
        "RecoveryCheckpoint": "checkpoint:present-before-start",
        "ReplicationTaskStats": {
            "FullLoadProgressPercent": 0,
            "ElapsedTimeMillis": 0,
            "TablesLoaded": 0,
            "TablesLoading": 0,
            "TablesQueued": 0,
            "TablesErrored": 0,
        },
    }
    change = dict(change)
    task["ReplicationTaskStats"].update(change.pop("stats", {}))
    missing = change.pop("missing_counter", None)
    if missing:
        task["ReplicationTaskStats"].pop(missing)
    task.update(change)

    dms = Mock()
    dms.describe_replication_tasks.return_value = {"ReplicationTasks": [task]}

    result = TaskInspection(dms).task_status(binding)

    assert result["has_started"] is expected
    dms.start_replication_task.assert_not_called()
