from dataclasses import replace

import pytest

from ingestion_registry.identity import identity_digest

from dms_runtime.control import TaskControl
from dms_runtime.operations import (
    RuntimeOperationConflict,
    RuntimeOperations,
)

from test_operations import operation_setup
from test_ownership import scope_setup
from test_persisted_control import persisted_setup


def test_stale_stop_cannot_stop_a_task_after_another_stop_resume_sequence(
    persisted_setup,
    monkeypatch,
):
    setup = persisted_setup

    setup["task"].update(
        status="running",
        has_started=True,
    )

    old_stop = replace(
        setup["request"],
        action="stop",
        operation_id="old-stop",
    )

    other_control = TaskControl(
        setup["dms"],
        setup["inspection"],
        setup["preflight"],
        check_scope=setup["guard"],
        credential_sync_function_arn=(
            "arn:aws:lambda:eu-west-2:"
            "111122223333:function:source-credential-sync"
        ),
        operations=RuntimeOperations(setup["guard"]),
    )

    original_begin = setup["operations"].begin

    def run_other_commands_before_old_reservation(
        request,
        binding,
        **kwargs,
    ):
        other_control.stop(
            binding,
            request=replace(
                setup["request"],
                action="stop",
                operation_id="new-stop",
            ),
        )

        setup["task"].update(
            status="stopped",
            has_started=True,
        )

        other_control.resume(
            binding,
            request=replace(
                setup["request"],
                action="resume",
                operation_id="new-resume",
            ),
        )

        setup["task"]["status"] = "running"

        return original_begin(
            request,
            binding,
            **kwargs,
        )

    monkeypatch.setattr(
        setup["operations"],
        "begin",
        run_other_commands_before_old_reservation,
    )

    with pytest.raises(RuntimeOperationConflict):
        setup["controller"].stop(
            setup["binding"],
            request=old_stop,
        )

    assert setup["dms"].stop_replication_task.call_count == 1
    assert setup["dms"].start_replication_task.call_count == 1


def test_replay_rejects_a_saved_result_with_a_changed_fingerprint(
    persisted_setup,
):
    setup = persisted_setup

    original = setup["controller"].start(
        setup["binding"],
        request=setup["request"],
    )

    registry = setup["registry"]

    registry.client.update_item(
        TableName=registry.table_name,
        Key=registry._encode(
            {
                "pk": (
                    f"INGESTION#{setup['request'].ingestion_id}"
                ),
                "sk": (
                    "RUNTIME_OPERATION#"
                    + identity_digest(
                        [setup["request"].operation_id]
                    )
                ),
            }
        ),
        UpdateExpression="SET #result = :result",
        ExpressionAttributeNames={
            "#result": "result",
        },
        ExpressionAttributeValues=registry._encode(
            {
                ":result": {
                    **original,
                    "replication_task_arn": "different-task",
                }
            }
        ),
    )

    with pytest.raises(RuntimeOperationConflict):
        setup["controller"].start(
            setup["binding"],
            request=setup["request"],
        )

    assert setup["dms"].start_replication_task.call_count == 1