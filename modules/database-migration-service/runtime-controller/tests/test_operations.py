from dataclasses import replace

import pytest
from botocore.exceptions import ClientError

from dms_runtime.operations import (
    RuntimeOperationConflict,
    RuntimeOperationPending,
    RuntimeOperations,
)
from dms_runtime.ownership import RuntimeOwnershipLost
from dms_runtime.requests import (
    IngestionOwner,
    RuntimeRequest,
    TaskOwner,
)

from test_ownership import scope_setup


@pytest.fixture
def operation_setup(scope_setup):
    registry, binding, clock, guard = scope_setup

    ingestion = guard.ingestion_claim
    task = guard.task_claim

    request = RuntimeRequest(
        action="start",
        ingestion_id=ingestion.ingestion_id,
        operation_id="start-one",
        ingestion_owner=IngestionOwner(
            execution_id=ingestion.execution_id,
            fencing_token=ingestion.fencing_token,
            attempt=ingestion.attempt,
        ),
        task_owner=TaskOwner(
            execution_id=task.execution_id,
            group_fencing_token=task.group_fencing_token,
            task_fencing_token=task.task_fencing_token,
        ),
    )

    return (
        registry,
        binding,
        clock,
        guard,
        request,
        RuntimeOperations(guard),
    )


def command_result(binding, action="start"):
    return {
        "action": action,
        "replication_task_arn": binding[
            "resources"
        ]["replication_task_arn"],
        "task_status": (
            "stopping" if action == "stop" else "starting"
        ),
        "outcome": "REQUESTED",
    }


def test_first_call_persists_dispatch_permission_and_task_gate(
    operation_setup,
):
    registry, binding, _, guard, request, operations = (
        operation_setup
    )

    admission = operations.begin(request, binding)
    record = admission.record

    assert admission.may_dispatch is True
    assert admission.dispatch_token
    assert record["status"] == "DISPATCHING"
    assert record["operation_id"] == request.operation_id
    assert record["ingestion_id"] == request.ingestion_id
    assert record["action"] == "start"
    assert record["scope_fingerprint"] == (
        guard.task_claim.scope_fingerprint
    )

    assert registry._read(
        record["pk"],
        record["sk"],
    ) == record

    gate = registry._read(
        guard.task_claim.task_key,
        "RUNTIME_COMMAND",
    )

    assert gate["operation_pk"] == record["pk"]
    assert gate["operation_sk"] == record["sk"]
    assert gate["dispatch_token"] == admission.dispatch_token


def test_duplicate_pending_operation_cannot_dispatch_again(
    operation_setup,
):
    _, binding, _, _, request, operations = operation_setup

    original = operations.begin(request, binding)
    duplicate = operations.begin(request, binding)

    assert duplicate.may_dispatch is False
    assert duplicate.dispatch_token is None
    assert duplicate.record == original.record
    assert duplicate.record["status"] == "DISPATCHING"


def test_start_boundary_is_saved_once_and_not_moved_by_retry(
    operation_setup,
):
    _, binding, clock, _, request, operations = operation_setup

    original = operations.begin(request, binding)
    clock["now"] += 10

    duplicate = operations.begin(request, binding)

    assert original.record["full_load_not_before"] == (
        "1970-01-01T00:16:40+00:00"
    )
    assert duplicate.record["full_load_not_before"] == (
        original.record["full_load_not_before"]
    )


@pytest.mark.parametrize("action", ["resume", "stop"])
def test_other_commands_do_not_create_a_full_load_boundary(
    operation_setup,
    action,
):
    _, binding, _, _, request, operations = operation_setup

    request = replace(
        request,
        action=action,
        operation_id=f"{action}-one",
    )

    admission = operations.begin(request, binding)

    assert admission.may_dispatch is True
    assert "full_load_not_before" not in admission.record


def test_same_operation_id_cannot_be_reused_for_another_action(
    operation_setup,
):
    _, binding, _, _, request, operations = operation_setup

    operations.begin(request, binding)

    with pytest.raises(RuntimeOperationConflict):
        operations.begin(
            replace(request, action="stop"),
            binding,
        )


def test_unresolved_command_blocks_a_different_operation(
    operation_setup,
):
    registry, binding, _, guard, request, operations = (
        operation_setup
    )

    original = operations.begin(request, binding)

    with pytest.raises(RuntimeOperationPending):
        operations.begin(
            replace(
                request,
                action="stop",
                operation_id="stop-one",
            ),
            binding,
        )

    gate = registry._read(
        guard.task_claim.task_key,
        "RUNTIME_COMMAND",
    )

    assert gate["dispatch_token"] == original.dispatch_token


def test_finish_saves_result_and_releases_task_gate(
    operation_setup,
):
    registry, binding, _, guard, request, operations = (
        operation_setup
    )

    admission = operations.begin(request, binding)
    result = command_result(binding)

    recorded = operations.finish(
        request,
        binding,
        dispatch_token=admission.dispatch_token,
        result=result,
    )

    assert recorded["status"] == "RECORDED"
    assert recorded["result"] == result
    assert recorded["full_load_not_before"] == (
        admission.record["full_load_not_before"]
    )

    assert registry._read(
        recorded["pk"],
        recorded["sk"],
    ) == recorded

    assert registry._read(
        guard.task_claim.task_key,
        "RUNTIME_COMMAND",
    ) is None


def test_delayed_retry_returns_original_result_without_dispatch(
    operation_setup,
):
    _, binding, clock, _, request, operations = operation_setup

    admission = operations.begin(request, binding)

    recorded = operations.finish(
        request,
        binding,
        dispatch_token=admission.dispatch_token,
        result=command_result(binding),
    )

    stop = replace(
        request,
        action="stop",
        operation_id="stop-one",
    )

    stop_admission = operations.begin(stop, binding)

    operations.finish(
        stop,
        binding,
        dispatch_token=stop_admission.dispatch_token,
        result=command_result(binding, "stop"),
    )

    clock["now"] += 10

    delayed = operations.begin(request, binding)

    assert delayed.may_dispatch is False
    assert delayed.dispatch_token is None
    assert delayed.record == recorded
    assert delayed.record["result"]["action"] == "start"


def test_repeating_finish_returns_the_original_record(
    operation_setup,
):
    _, binding, clock, _, request, operations = operation_setup

    admission = operations.begin(request, binding)
    result = command_result(binding)

    original = operations.finish(
        request,
        binding,
        dispatch_token=admission.dispatch_token,
        result=result,
    )

    clock["now"] += 10

    duplicate = operations.finish(
        request,
        binding,
        dispatch_token=admission.dispatch_token,
        result=result,
    )

    assert duplicate == original


def test_saved_result_cannot_be_replaced(
    operation_setup,
):
    _, binding, _, _, request, operations = operation_setup

    admission = operations.begin(request, binding)
    result = command_result(binding)

    operations.finish(
        request,
        binding,
        dispatch_token=admission.dispatch_token,
        result=result,
    )

    with pytest.raises(RuntimeOperationConflict):
        operations.finish(
            request,
            binding,
            dispatch_token=admission.dispatch_token,
            result={
                **result,
                "outcome": "ALREADY_RUNNING",
            },
        )


def test_wrong_dispatch_token_cannot_save_result_or_release_gate(
    operation_setup,
):
    registry, binding, _, guard, request, operations = (
        operation_setup
    )

    admission = operations.begin(request, binding)

    with pytest.raises(RuntimeOperationConflict):
        operations.finish(
            request,
            binding,
            dispatch_token="wrong-token",
            result=command_result(binding),
        )

    gate = registry._read(
        guard.task_claim.task_key,
        "RUNTIME_COMMAND",
    )

    assert gate["dispatch_token"] == admission.dispatch_token

    assert registry._read(
        admission.record["pk"],
        admission.record["sk"],
    )["status"] == "DISPATCHING"


def test_finish_requires_an_existing_reservation(
    operation_setup,
):
    _, binding, _, _, request, operations = operation_setup

    with pytest.raises(
        RuntimeOperationConflict,
        match="not been reserved",
    ):
        operations.finish(
            request,
            binding,
            dispatch_token="unreserved-token",
            result=command_result(binding),
        )


def test_expired_ownership_cannot_reserve_a_command(
    operation_setup,
):
    registry, binding, clock, guard, request, operations = (
        operation_setup
    )

    clock["now"] += 300

    with pytest.raises(RuntimeOwnershipLost):
        operations.begin(request, binding)

    assert registry._read(
        guard.task_claim.task_key,
        "RUNTIME_COMMAND",
    ) is None


def test_expired_ownership_cannot_replay_a_saved_result(
    operation_setup,
):
    _, binding, clock, _, request, operations = operation_setup

    admission = operations.begin(request, binding)

    operations.finish(
        request,
        binding,
        dispatch_token=admission.dispatch_token,
        result=command_result(binding),
    )

    clock["now"] += 300

    with pytest.raises(RuntimeOwnershipLost):
        operations.begin(request, binding)


def test_expired_ownership_cannot_save_result_or_release_gate(
    operation_setup,
):
    registry, binding, clock, guard, request, operations = (
        operation_setup
    )

    admission = operations.begin(request, binding)
    clock["now"] += 300

    with pytest.raises(RuntimeOwnershipLost):
        operations.finish(
            request,
            binding,
            dispatch_token=admission.dispatch_token,
            result=command_result(binding),
        )

    assert registry._read(
        guard.task_claim.task_key,
        "RUNTIME_COMMAND",
    ) is not None

    assert registry._read(
        admission.record["pk"],
        admission.record["sk"],
    )["status"] == "DISPATCHING"


@pytest.mark.parametrize(
    "owner",
    ["ingestion", "task"],
)
def test_request_must_match_the_supplied_ownership_claims(
    operation_setup,
    owner,
):
    registry, binding, _, guard, request, operations = (
        operation_setup
    )

    if owner == "ingestion":
        request = replace(
            request,
            ingestion_owner=replace(
                request.ingestion_owner,
                fencing_token=99,
            ),
        )
    else:
        request = replace(
            request,
            task_owner=replace(
                request.task_owner,
                task_fencing_token=99,
            ),
        )

    with pytest.raises(
        ValueError,
        match="ownership does not match",
    ):
        operations.begin(request, binding)

    assert registry._read(
        guard.task_claim.task_key,
        "RUNTIME_COMMAND",
    ) is None


def test_permission_errors_are_not_hidden(
    operation_setup,
    monkeypatch,
):
    registry, binding, _, _, request, operations = (
        operation_setup
    )

    original_transaction = (
        registry.client.transact_write_items
    )

    def deny_operation_write(**kwargs):
        if any(
            "Put" in action
            for action in kwargs["TransactItems"]
        ):
            raise ClientError(
                {
                    "Error": {
                        "Code": "AccessDeniedException",
                    }
                },
                "TransactWriteItems",
            )

        return original_transaction(**kwargs)

    monkeypatch.setattr(
        registry.client,
        "transact_write_items",
        deny_operation_write,
    )

    with pytest.raises(ClientError) as caught:
        operations.begin(request, binding)

    assert caught.value.response["Error"]["Code"] == (
        "AccessDeniedException"
    )


def test_ownership_is_checked_in_the_reservation_transaction(
    operation_setup,
    monkeypatch,
):
    registry, binding, clock, guard, request, operations = (
        operation_setup
    )

    original_checks = guard.transaction_checks
    calls = 0

    def expire_before_reservation(binding, action):
        nonlocal calls
        calls += 1

        if calls == 2:
            clock["now"] += 300

        return original_checks(binding, action)

    monkeypatch.setattr(
        guard,
        "transaction_checks",
        expire_before_reservation,
    )

    with pytest.raises(RuntimeOwnershipLost):
        operations.begin(request, binding)

    assert registry._read(
        guard.task_claim.task_key,
        "RUNTIME_COMMAND",
    ) is None


def test_ownership_is_checked_in_the_result_transaction(
    operation_setup,
    monkeypatch,
):
    registry, binding, clock, guard, request, operations = (
        operation_setup
    )

    admission = operations.begin(request, binding)
    original_checks = guard.transaction_checks
    calls = 0

    def expire_before_result_write(binding, action):
        nonlocal calls
        calls += 1

        if calls == 2:
            clock["now"] += 300

        return original_checks(binding, action)

    monkeypatch.setattr(
        guard,
        "transaction_checks",
        expire_before_result_write,
    )

    with pytest.raises(RuntimeOwnershipLost):
        operations.finish(
            request,
            binding,
            dispatch_token=admission.dispatch_token,
            result=command_result(binding),
        )

    assert registry._read(
        admission.record["pk"],
        admission.record["sk"],
    )["status"] == "DISPATCHING"

    assert registry._read(
        guard.task_claim.task_key,
        "RUNTIME_COMMAND",
    ) is not None