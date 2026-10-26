import pytest
from botocore.exceptions import ReadTimeoutError

from test_operations import (
    command_result,
    operation_setup,
)
from test_ownership import scope_setup


def test_lost_reservation_response_does_not_allow_retry_to_dispatch(
    operation_setup,
    monkeypatch,
):
    registry, binding, clock, guard, request, operations = (
        operation_setup
    )

    original_transaction = (
        registry.client.transact_write_items
    )
    lost_response = False

    def commit_then_lose_response(**kwargs):
        nonlocal lost_response

        response = original_transaction(**kwargs)

        if (
            not lost_response
            and any(
                "Put" in action
                for action in kwargs["TransactItems"]
            )
        ):
            lost_response = True
            raise ReadTimeoutError(
                endpoint_url=(
                    "https://dynamodb.eu-west-2.amazonaws.com"
                )
            )

        return response

    monkeypatch.setattr(
        registry.client,
        "transact_write_items",
        commit_then_lose_response,
    )

    with pytest.raises(ReadTimeoutError):
        operations.begin(request, binding)

    gate = registry._read(
        guard.task_claim.task_key,
        "RUNTIME_COMMAND",
    )

    original = registry._read(
        gate["operation_pk"],
        gate["operation_sk"],
    )

    clock["now"] += 10
    retry = operations.begin(request, binding)

    assert retry.may_dispatch is False
    assert retry.dispatch_token is None
    assert retry.record == original
    assert retry.record["status"] == "DISPATCHING"
    assert retry.record["full_load_not_before"] == (
        "1970-01-01T00:16:40+00:00"
    )


def test_lost_result_response_returns_saved_result_on_retry(
    operation_setup,
    monkeypatch,
):
    registry, binding, clock, guard, request, operations = (
        operation_setup
    )

    admission = operations.begin(request, binding)
    result = command_result(binding)

    original_transaction = (
        registry.client.transact_write_items
    )
    lost_response = False

    def commit_then_lose_response(**kwargs):
        nonlocal lost_response

        response = original_transaction(**kwargs)

        if (
            not lost_response
            and any(
                "Delete" in action
                for action in kwargs["TransactItems"]
            )
        ):
            lost_response = True
            raise ReadTimeoutError(
                endpoint_url=(
                    "https://dynamodb.eu-west-2.amazonaws.com"
                )
            )

        return response

    monkeypatch.setattr(
        registry.client,
        "transact_write_items",
        commit_then_lose_response,
    )

    with pytest.raises(ReadTimeoutError):
        operations.finish(
            request,
            binding,
            dispatch_token=admission.dispatch_token,
            result=result,
        )

    original = registry._read(
        admission.record["pk"],
        admission.record["sk"],
    )

    assert original["status"] == "RECORDED"
    assert registry._read(
        guard.task_claim.task_key,
        "RUNTIME_COMMAND",
    ) is None

    clock["now"] += 10

    retry = operations.finish(
        request,
        binding,
        dispatch_token=admission.dispatch_token,
        result=result,
    )

    assert retry == original

    replay = operations.begin(request, binding)

    assert replay.may_dispatch is False
    assert replay.record == original


def test_failed_result_write_keeps_operation_and_gate_pending(
    operation_setup,
    monkeypatch,
):
    registry, binding, _, guard, request, operations = (
        operation_setup
    )

    admission = operations.begin(request, binding)
    original_transaction = (
        registry.client.transact_write_items
    )

    def lose_response_before_write(**kwargs):
        if any(
            "Delete" in action
            for action in kwargs["TransactItems"]
        ):
            raise ReadTimeoutError(
                endpoint_url=(
                    "https://dynamodb.eu-west-2.amazonaws.com"
                )
            )

        return original_transaction(**kwargs)

    monkeypatch.setattr(
        registry.client,
        "transact_write_items",
        lose_response_before_write,
    )

    with pytest.raises(ReadTimeoutError):
        operations.finish(
            request,
            binding,
            dispatch_token=admission.dispatch_token,
            result=command_result(binding),
        )

    retry = operations.begin(request, binding)

    assert retry.may_dispatch is False
    assert retry.record["status"] == "DISPATCHING"

    gate = registry._read(
        guard.task_claim.task_key,
        "RUNTIME_COMMAND",
    )

    assert gate["dispatch_token"] == admission.dispatch_token


def test_competing_duplicate_gets_only_one_dispatch_permission(
    operation_setup,
    monkeypatch,
):
    registry, binding, _, _, request, operations = (
        operation_setup
    )

    original_transaction = (
        registry.client.transact_write_items
    )
    competitor = None

    def competing_reservation(**kwargs):
        nonlocal competitor

        if (
            competitor is None
            and any(
                "Put" in action
                for action in kwargs["TransactItems"]
            )
        ):
            monkeypatch.setattr(
                registry.client,
                "transact_write_items",
                original_transaction,
            )

            competitor = operations.begin(request, binding)

        return original_transaction(**kwargs)

    monkeypatch.setattr(
        registry.client,
        "transact_write_items",
        competing_reservation,
    )

    original_caller = operations.begin(request, binding)

    assert competitor is not None
    assert competitor.may_dispatch is True
    assert original_caller.may_dispatch is False
    assert original_caller.record == competitor.record