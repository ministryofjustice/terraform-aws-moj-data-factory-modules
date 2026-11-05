"""Integration tests for current-contract activation."""

import copy

import pytest

from contract_integration_support import (
    approve,
    complete_registration_integration,
    expected_contract_id,
)
from ingestion_registry.contract_activation import (
    ContractActivation,
    ContractActivationConflict,
)
from ingestion_registry.contract_approval import ContractApprovalBlocked


def activation_service(setup):
    return ContractActivation(
        completion=setup["completion"],
        lifecycle=setup["lifecycle"],
    )


def activate_contract(setup, service):
    return service.activate(
        table=setup["table"],
        request_id="request-one",
        contract_version="v1",
    )


def test_registration_alone_does_not_select_current_contract(
    registration_integration,
):
    setup = registration_integration
    approve(setup)
    complete_registration_integration(setup)

    service = activation_service(setup)

    assert service.read_current(table=setup["table"]) is None


def test_approved_registered_contract_can_be_activated(
    registration_integration,
):
    setup = registration_integration
    approve(setup)
    service = activation_service(setup)

    activated = activate_contract(setup, service)
    current = service.read_current(table=setup["table"])

    assert current == activated["selection"]
    assert current["revision"] == 1
    assert current["registered_contract"]["contract_version"] == "v1"
    assert current["approval_reference"] == (
        activated["registration_result"]["approval_reference"]
    )

    # Activation does not change the existing registration handler's
    # field. CONTRACT_CURRENT is the authoritative selection.
    item = setup["registration_table"].get_item(
        Key={
            "contract_id": expected_contract_id(setup["table"]),
            "contract_version": "v1",
        },
        ConsistentRead=True,
    )["Item"]
    assert item["is_active"] is False


def test_activation_retry_preserves_selection_revision(
    registration_integration,
):
    setup = registration_integration
    approve(setup)
    service = activation_service(setup)

    first = activate_contract(setup, service)
    second = activate_contract(setup, service)

    assert second == first
    assert service.read_current(
        table=setup["table"]
    )["revision"] == 1
    assert setup["invocation"].calls == 1


def test_first_contract_approval_cannot_replace_existing_selection(
    registration_integration,
):
    setup = registration_integration
    approve(setup)
    service = activation_service(setup)
    registry = setup["registry"]

    existing = {
        "pk": setup["table"].partition_key,
        "sk": "CONTRACT_CURRENT",
        "record_type": "CONTRACT_CURRENT",
        "revision": 1,
        "marker": "another activation already won",
    }
    registry.client.put_item(
        TableName=registry.table_name,
        Item=registry._encode(existing),
    )

    with pytest.raises(
        ContractActivationConflict,
        match="current contract already exists",
    ):
        activate_contract(setup, service)

    assert service.read_current(table=setup["table"]) == existing
    assert registry._read(
        setup["request"]["pk"], "ACTIVATION"
    ) is None


def test_completed_activation_retry_does_not_restore_old_selection(
    registration_integration,
):
    setup = registration_integration
    approve(setup)
    service = activation_service(setup)

    first = activate_contract(setup, service)

    # Simulate a later selection. Retrying the completed request
    # must not write the old selection back.
    later = copy.deepcopy(first["selection"])
    later["revision"] = 2
    later["request_key"] = "CONTRACTREQUEST#later"

    registry = setup["registry"]
    registry.client.put_item(
        TableName=registry.table_name,
        Item=registry._encode(later),
    )

    retried = activate_contract(setup, service)

    assert retried == first
    assert service.read_current(table=setup["table"]) == later


def test_pending_review_cannot_activate(
    registration_integration,
):
    setup = registration_integration
    service = activation_service(setup)

    with pytest.raises(
        ContractApprovalBlocked,
        match="not currently approved",
    ):
        activate_contract(setup, service)

    assert service.read_current(table=setup["table"]) is None
    assert setup["invocation"].calls == 0
