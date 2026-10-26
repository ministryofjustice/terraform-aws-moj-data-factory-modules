from dataclasses import asdict, replace

import pytest

from dms_runtime.control import TaskOperationBlocked
from dms_runtime.guard_factory import RuntimeGuardFactory
from dms_runtime.handler import RuntimeHandler
from dms_runtime.owned_actions import OwnedRuntimeActions
from dms_runtime.ownership import RuntimeOwnershipLost

from test_operations import operation_setup
from test_ownership import scope_setup
from test_persisted_control import persisted_setup


SYNC_ARN = (
    "arn:aws:lambda:eu-west-2:"
    "111122223333:function:source-credential-sync"
)


def make_adapter(setup, *, sync_arn=SYNC_ARN):
    factory = RuntimeGuardFactory(
        setup["registry"],
        context=setup["guard"].context,
        clock=lambda: setup["clock"]["now"],
    )

    return OwnedRuntimeActions(
        guard_factory=factory,
        dms_client=setup["dms"],
        inspection=setup["inspection"],
        preflight=setup["preflight"],
        credential_sync_function_arn=sync_arn,
    )


def test_adapter_starts_and_replays_without_another_command(
    persisted_setup,
):
    setup = persisted_setup
    adapter = make_adapter(setup)

    original = adapter(
        setup["request"],
        setup["binding"],
    )

    setup["task"].update(
        status="stopped",
        has_started=True,
    )

    retry = adapter(
        setup["request"],
        setup["binding"],
    )

    assert retry == original
    assert original["outcome"] == "REQUESTED"
    setup["dms"].start_replication_task.assert_called_once()

    assert setup["preflight"].run.call_args.kwargs[
        "credential_sync_function_arn"
    ] == SYNC_ARN


@pytest.mark.parametrize("action", ["resume", "stop"])
def test_adapter_dispatches_other_commands(
    persisted_setup,
    action,
):
    setup = persisted_setup
    adapter = make_adapter(setup)

    setup["task"].update(
        status="stopped" if action == "resume" else "running",
        has_started=True,
    )

    result = adapter(
        replace(
            setup["request"],
            action=action,
            operation_id=f"{action}-one",
        ),
        setup["binding"],
    )

    assert result["action"] == action
    assert result["outcome"] == "REQUESTED"

    if action == "resume":
        setup["dms"].start_replication_task.assert_called_once_with(
            ReplicationTaskArn=setup["task"][
                "replication_task_arn"
            ],
            StartReplicationTaskType="resume-processing",
        )
    else:
        setup["dms"].stop_replication_task.assert_called_once()
        setup["preflight"].run.assert_not_called()


@pytest.mark.parametrize("sync_arn", [SYNC_ARN, None])
def test_standalone_preflight_forwards_explicit_sync_configuration(
    persisted_setup,
    sync_arn,
):
    setup = persisted_setup
    adapter = make_adapter(setup, sync_arn=sync_arn)

    result = adapter(
        replace(
            setup["request"],
            action="preflight",
            operation_id="preflight-one",
        ),
        setup["binding"],
    )

    assert result["outcome"] == "SUCCEEDED"
    assert result["action"] == "preflight"

    assert setup["preflight"].run.call_args.kwargs[
        "credential_sync_function_arn"
    ] == sync_arn

    setup["dms"].start_replication_task.assert_not_called()
    setup["dms"].stop_replication_task.assert_not_called()


def test_standalone_preflight_is_fresh_on_each_invocation(
    persisted_setup,
):
    setup = persisted_setup
    adapter = make_adapter(setup)

    request = replace(
        setup["request"],
        action="preflight",
        operation_id="preflight-one",
    )

    adapter(request, setup["binding"])

    setup["preflight"].run.side_effect = RuntimeError(
        "Rotated credentials unavailable"
    )

    with pytest.raises(
        RuntimeError,
        match="Rotated credentials unavailable",
    ):
        adapter(request, setup["binding"])

    assert setup["preflight"].run.call_count == 2


def test_unsuccessful_preflight_cannot_report_success(
    persisted_setup,
):
    setup = persisted_setup
    adapter = make_adapter(setup)

    setup["preflight"].run.return_value = {
        "connectionStatus": "testing",
    }

    with pytest.raises(
        TaskOperationBlocked,
        match="preflight has not succeeded",
    ):
        adapter(
            replace(
                setup["request"],
                action="preflight",
                operation_id="preflight-one",
            ),
            setup["binding"],
        )


def test_expired_ownership_prevents_preflight(
    persisted_setup,
):
    setup = persisted_setup
    adapter = make_adapter(setup)

    setup["clock"]["now"] += 300

    with pytest.raises(RuntimeOwnershipLost):
        adapter(
            replace(
                setup["request"],
                action="preflight",
                operation_id="preflight-one",
            ),
            setup["binding"],
        )

    setup["preflight"].run.assert_not_called()


def test_ownership_lost_during_preflight_prevents_success(
    persisted_setup,
):
    setup = persisted_setup
    adapter = make_adapter(setup)

    def expire_during_preflight(**kwargs):
        setup["clock"]["now"] += 300
        return {"connectionStatus": "successful"}

    setup["preflight"].run.side_effect = expire_during_preflight

    with pytest.raises(RuntimeOwnershipLost):
        adapter(
            replace(
                setup["request"],
                action="preflight",
                operation_id="preflight-one",
            ),
            setup["binding"],
        )


@pytest.mark.parametrize(
    "sync_arn",
    [
        "",
        "untrusted-function",
        (
            "arn:aws:lambda:eu-west-1:"
            "111122223333:function:source-sync"
        ),
        (
            "arn:aws:lambda:eu-west-2:"
            "999900001111:function:source-sync"
        ),
    ],
)
def test_sync_reference_must_match_authorised_context(
    persisted_setup,
    sync_arn,
):
    with pytest.raises(ValueError, match="Credential sync"):
        make_adapter(
            persisted_setup,
            sync_arn=sync_arn,
        )


def test_shared_handler_connects_to_real_owned_adapter(
    persisted_setup,
):
    from unittest.mock import Mock

    setup = persisted_setup
    adapter = make_adapter(setup)

    lookup = Mock()
    lookup.registry = setup["registry"]
    lookup.resolve.return_value = setup["binding"]

    handler = RuntimeHandler(
        lookup=lookup,
        inspection=setup["inspection"],
        full_load=Mock(),
        owned_action_handler=adapter,
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

    original = handler.handle(event)
    retry = handler.handle(event)

    assert original["outcome"] == "REQUESTED"
    assert retry == original
    setup["dms"].start_replication_task.assert_called_once()