from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from dms_runtime.inspection import (
    InspectionError,
    TaskInspection,
)


TASK = "arn:aws:dms:eu-west-2:111122223333:task:TASK"
INSTANCE = "arn:aws:dms:eu-west-2:111122223333:rep:INSTANCE"
SOURCE = "arn:aws:dms:eu-west-2:111122223333:endpoint:SOURCE"
TARGET = "arn:aws:dms:eu-west-2:111122223333:endpoint:TARGET"


@pytest.fixture
def inspection_setup():
    dms = Mock()

    binding = {
        "resources": {
            "replication_task_arn": TASK,
            "replication_instance_arn": INSTANCE,
            "source_endpoint_arn": SOURCE,
            "target_endpoint_arn": TARGET,
        },
        "task_group": {
            "migration_type": "full-load-and-cdc",
        },
    }

    task = {
        "ReplicationTaskArn": TASK,
        "ReplicationInstanceArn": INSTANCE,
        "SourceEndpointArn": SOURCE,
        "TargetEndpointArn": TARGET,
        "MigrationType": "full-load-and-cdc",
        "Status": "running",
        "ReplicationTaskStats": {
            "FullLoadProgressPercent": 100,
        },
    }

    dms.describe_replication_tasks.return_value = {
        "ReplicationTasks": [task],
    }
    dms.describe_table_statistics.return_value = {
        "ReplicationTaskArn": TASK,
        "TableStatistics": [],
    }

    return dms, binding, task, TaskInspection(dms)


def test_task_status_is_observed_without_claiming_completion(
    inspection_setup,
):
    dms, binding, _, inspection = inspection_setup

    result = inspection.task_status(binding)

    assert result["status"] == "running"
    assert result["full_load_progress_percent"] == 100
    assert "full_load_ready" not in result

    dms.start_replication_task.assert_not_called()
    dms.stop_replication_task.assert_not_called()


@pytest.mark.parametrize(
    "field",
    [
        "ReplicationTaskArn",
        "ReplicationInstanceArn",
        "SourceEndpointArn",
        "TargetEndpointArn",
        "MigrationType",
    ],
)
def test_task_binding_mismatch_is_rejected(
    inspection_setup,
    field,
):
    _, binding, task, inspection = inspection_setup
    task[field] = "another-resource"

    with pytest.raises(InspectionError, match="pinned binding"):
        inspection.task_status(binding)


@pytest.mark.parametrize("count", [0, 2])
def test_missing_or_ambiguous_task_is_rejected(
    inspection_setup,
    count,
):
    dms, binding, task, inspection = inspection_setup

    dms.describe_replication_tasks.return_value = {
        "ReplicationTasks": [task] * count,
    }

    with pytest.raises(InspectionError, match="exactly one"):
        inspection.task_status(binding)


def test_table_page_preserves_case_and_serialises_dates(
    inspection_setup,
):
    dms, binding, _, inspection = inspection_setup

    finished = datetime(
        2026,
        10,
        26,
        9,
        0,
        tzinfo=timezone.utc,
    )

    dms.describe_table_statistics.return_value = {
        "ReplicationTaskArn": TASK,
        "TableStatistics": [
            {
                "SchemaName": "DMS_USER",
                "TableName": "EXAMPLE",
                "TableState": "Table completed",
                "FullLoadEndTime": finished,
            }
        ],
        "Marker": "page-two",
    }

    result = inspection.table_status_page(binding)

    assert result["tables"][0]["schema_name"] == "DMS_USER"
    assert result["tables"][0]["full_load_finished_at"] == (
        finished.isoformat()
    )
    assert result["next_marker"] == "page-two"
    assert dms.describe_table_statistics.call_count == 1


def test_next_page_uses_the_same_task(inspection_setup):
    dms, binding, _, inspection = inspection_setup

    result = inspection.table_status_page(
        binding,
        marker="page-two",
    )

    dms.describe_table_statistics.assert_called_once_with(
        ReplicationTaskArn=TASK,
        MaxRecords=100,
        Marker="page-two",
    )

    assert result["next_marker"] is None


def test_repeated_marker_is_rejected(inspection_setup):
    dms, binding, _, inspection = inspection_setup
    dms.describe_table_statistics.return_value["Marker"] = "same-page"

    with pytest.raises(InspectionError, match="repeated"):
        inspection.table_status_page(
            binding,
            marker="same-page",
        )


def test_statistics_for_another_task_are_rejected(
    inspection_setup,
):
    dms, binding, _, inspection = inspection_setup

    dms.describe_table_statistics.return_value[
        "ReplicationTaskArn"
    ] = "another-task"

    with pytest.raises(InspectionError, match="different task"):
        inspection.table_status_page(binding)


def test_duplicate_table_rows_are_rejected(inspection_setup):
    dms, binding, _, inspection = inspection_setup

    dms.describe_table_statistics.return_value["TableStatistics"] = [
        {
            "SchemaName": "public",
            "TableName": "example",
        }
    ] * 2

    with pytest.raises(InspectionError, match="duplicate"):
        inspection.table_status_page(binding)


def test_missing_binding_fails_before_calling_dms(
    inspection_setup,
):
    dms, _, _, inspection = inspection_setup

    with pytest.raises(ValueError, match="complete resolved"):
        inspection.task_status({})

    dms.describe_replication_tasks.assert_not_called()


def test_naive_timestamp_is_rejected(inspection_setup):
    dms, binding, _, inspection = inspection_setup

    dms.describe_table_statistics.return_value["TableStatistics"] = [
        {
            "SchemaName": "public",
            "TableName": "example",
            "FullLoadEndTime": datetime(2026, 10, 26),
        }
    ]

    with pytest.raises(InspectionError, match="timezone-aware"):
        inspection.table_status_page(binding)