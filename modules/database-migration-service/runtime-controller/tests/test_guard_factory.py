from dataclasses import replace
from unittest.mock import Mock

import pytest

from dms_runtime.guard_factory import RuntimeGuardFactory
from dms_runtime.ownership import (
    RuntimeOwnershipLost,
    RuntimeScopeDenied,
)
from dms_runtime.requests import RuntimeRequest

from test_operations import operation_setup
from test_ownership import scope_setup


@pytest.fixture
def factory_setup(operation_setup):
    registry, binding, clock, guard, request, _ = (
        operation_setup
    )

    factory = RuntimeGuardFactory(
        registry,
        context=guard.context,
        clock=lambda: clock["now"],
    )

    return registry, binding, clock, guard, request, factory


@pytest.mark.parametrize(
    "action",
    ["preflight", "start", "resume", "stop"],
)
def test_factory_validates_existing_claims_without_changing_them(
    factory_setup,
    action,
):
    registry, binding, _, original, request, factory = (
        factory_setup
    )

    keys = [
        f"INGESTION#{request.ingestion_id}",
        original.task_claim.group_key,
        original.task_claim.task_key,
    ]

    before = [
        registry._read(key, "OWNERSHIP")
        for key in keys
    ]

    guard = factory(
        replace(request, action=action),
        binding,
    )

    assert guard.ingestion_claim == original.ingestion_claim
    assert guard.task_claim == original.task_claim

    assert [
        registry._read(key, "OWNERSHIP")
        for key in keys
    ] == before


@pytest.mark.parametrize(
    "owner",
    ["ingestion", "group", "task"],
)
def test_factory_rejects_expired_ownership(
    factory_setup,
    owner,
):
    registry, binding, clock, guard, request, factory = (
        factory_setup
    )

    pk = {
        "ingestion": f"INGESTION#{request.ingestion_id}",
        "group": guard.task_claim.group_key,
        "task": guard.task_claim.task_key,
    }[owner]

    registry.client.update_item(
        TableName=registry.table_name,
        Key=registry._encode(
            {"pk": pk, "sk": "OWNERSHIP"}
        ),
        UpdateExpression="SET lease_expires_at = :now",
        ExpressionAttributeValues=registry._encode(
            {":now": clock["now"]}
        ),
    )

    with pytest.raises(RuntimeOwnershipLost):
        factory(request, binding)


@pytest.mark.parametrize(
    "token",
    [
        "ingestion",
        "group",
        "task",
    ],
)
def test_factory_does_not_replace_stale_request_tokens(
    factory_setup,
    token,
):
    _, binding, _, _, request, factory = factory_setup

    if token == "ingestion":
        request = replace(
            request,
            ingestion_owner=replace(
                request.ingestion_owner,
                fencing_token=99,
            ),
        )
    else:
        field = (
            "group_fencing_token"
            if token == "group"
            else "task_fencing_token"
        )

        request = replace(
            request,
            task_owner=replace(
                request.task_owner,
                **{field: 99},
            ),
        )

    with pytest.raises(RuntimeOwnershipLost):
        factory(request, binding)


def test_factory_rejects_other_service_before_registry_reads(
    factory_setup,
    monkeypatch,
):
    registry, binding, _, _, request, factory = (
        factory_setup
    )

    read = Mock()
    monkeypatch.setattr(registry, "_read", read)

    with pytest.raises(RuntimeScopeDenied, match="source"):
        factory(
            request,
            {
                **binding,
                "source": "another-service",
            },
        )

    read.assert_not_called()


def test_factory_rejects_read_requests(factory_setup):
    _, binding, _, _, request, factory = factory_setup

    read_request = RuntimeRequest(
        action="task_status",
        ingestion_id=request.ingestion_id,
    )

    with pytest.raises(ValueError, match="owned runtime request"):
        factory(read_request, binding)


def test_factory_does_not_claim_unknown_ingestion(factory_setup):
    _, binding, _, _, request, factory = factory_setup

    with pytest.raises(
        RuntimeOwnershipLost,
        match="Existing ingestion and task ownership",
    ):
        factory(
            replace(
                request,
                ingestion_id="unknown-ingestion",
            ),
            binding,
        )