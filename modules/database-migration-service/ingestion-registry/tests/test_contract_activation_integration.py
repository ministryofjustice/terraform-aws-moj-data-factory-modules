import copy

import pytest

from contract_integration_support import (
    approve,
    complete_registration_integration,
    expected_contract_id,
    prepare_approved_contract_change,
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

def test_approved_change_replaces_exact_current_baseline(
    registration_integration,
):
    setup = registration_integration
    approve(setup)
    service = activation_service(setup)
    original = activate_contract(setup, service)["selection"]

    prepare_approved_contract_change(
        setup,
        request_id="request-two",
        contract_version="v2",
        baseline_selection=original,
    )

    activated = service.activate(
        table=setup["table"],
        request_id="request-two",
        contract_version="v2",
    )
    current = service.read_current(table=setup["table"])

    assert current == activated["selection"]
    assert current["revision"] == 2
    assert current["registered_contract"]["contract_version"] == "v2"

    # Activation preserves the original immutable history.
    original_activation = setup["registry"]._read(
        setup["request"]["pk"],
        "ACTIVATION",
    )
    assert original_activation["selection"] == original

    original_registration = setup["registration_table"].get_item(
        Key={
            "contract_id": expected_contract_id(setup["table"]),
            "contract_version": "v1",
        },
        ConsistentRead=True,
    )["Item"]
    assert original_registration["s3_object_version_id"] == (
        original["registered_contract"]["object_reference"]["version_id"]
    )


def test_stale_comparison_baseline_blocks_activation(
    registration_integration,
):
    setup = registration_integration
    approve(setup)
    service = activation_service(setup)
    original = activate_contract(setup, service)["selection"]

    prepare_approved_contract_change(
        setup,
        request_id="request-two",
        contract_version="v2",
        baseline_selection=original,
    )
    stale_request = prepare_approved_contract_change(
        setup,
        request_id="request-three",
        contract_version="v3",
        baseline_selection=original,
    )

    winner = service.activate(
        table=setup["table"],
        request_id="request-two",
        contract_version="v2",
    )

    with pytest.raises(
        ContractActivationConflict,
        match="baseline is no longer current",
    ):
        service.activate(
            table=setup["table"],
            request_id="request-three",
            contract_version="v3",
        )

    assert service.read_current(
        table=setup["table"]
    ) == winner["selection"]
    assert setup["registry"]._read(
        stale_request["pk"], "ACTIVATION"
    ) is None


def test_competing_activation_between_read_and_write_is_blocked(
    registration_integration, monkeypatch
):
    setup = registration_integration
    approve(setup)
    service = activation_service(setup)
    original = activate_contract(setup, service)["selection"]

    losing_request = prepare_approved_contract_change(
        setup,
        request_id="request-two",
        contract_version="v2",
        baseline_selection=original,
    )
    prepare_approved_contract_change(
        setup,
        request_id="request-three",
        contract_version="v3",
        baseline_selection=original,
    )

    original_commit = service._commit
    competing_service = activation_service(setup)
    winner = {}

    def commit_after_competing_activation(actions):
        # This runs after the losing operation read CONTRACT_CURRENT,
        # but before its conditional transaction is submitted.
        winner["result"] = competing_service.activate(
            table=setup["table"],
            request_id="request-three",
            contract_version="v3",
        )
        return original_commit(actions)

    monkeypatch.setattr(
        service,
        "_commit",
        commit_after_competing_activation,
    )

    with pytest.raises(
        ContractActivationConflict,
        match="changed concurrently",
    ):
        service.activate(
            table=setup["table"],
            request_id="request-two",
            contract_version="v2",
        )

    assert service.read_current(
        table=setup["table"]
    ) == winner["result"]["selection"]
    assert setup["registry"]._read(
        losing_request["pk"], "ACTIVATION"
    ) is None


def test_old_activation_retry_preserves_a_real_newer_activation(
    registration_integration,
):
    setup = registration_integration
    approve(setup)
    service = activation_service(setup)
    original = activate_contract(setup, service)["selection"]

    prepare_approved_contract_change(
        setup,
        request_id="request-two",
        contract_version="v2",
        baseline_selection=original,
    )
    second = service.activate(
        table=setup["table"],
        request_id="request-two",
        contract_version="v2",
    )

    prepare_approved_contract_change(
        setup,
        request_id="request-three",
        contract_version="v3",
        baseline_selection=second["selection"],
    )
    third = service.activate(
        table=setup["table"],
        request_id="request-three",
        contract_version="v3",
    )

    calls_before_retry = setup["invocation"].calls

    retried = service.activate(
        table=setup["table"],
        request_id="request-two",
        contract_version="v2",
    )

    assert retried == second
    assert service.read_current(
        table=setup["table"]
    ) == third["selection"]
    assert third["selection"]["revision"] == 3
    assert setup["invocation"].calls == calls_before_retry