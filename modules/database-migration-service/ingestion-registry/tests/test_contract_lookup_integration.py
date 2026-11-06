import copy

import pytest

from contract_integration_support import (
    approve,
    complete_registration_integration,
    prepare_approved_contract_change,
)
from ingestion_registry.contract_activation import ContractActivation
from ingestion_registry.contract_lookup import (
    ContractLookupBlocked,
    CurrentContractLookup,
)
from ingestion_registry.contract_verification import (
    ContractVerificationError,
)


def services(setup):
    activation = ContractActivation(
        completion=setup["completion"],
        lifecycle=setup["lifecycle"],
    )
    registrar = setup["completion"].registrar
    lookup = CurrentContractLookup(
        registry=setup["registry"],
        approval_gate=registrar.approval_gate,
        verifier=registrar.verifier,
    )
    return activation, lookup


def activate_initial(setup, activation):
    approve(setup)
    return activation.activate(
        table=setup["table"],
        request_id="request-one",
        contract_version="v1",
    )


def test_lookup_returns_none_without_current_selection(
    registration_integration,
):
    setup = registration_integration
    _, lookup = services(setup)

    assert lookup.resolve(table=setup["table"]) is None
    assert setup["invocation"].calls == 0


def test_registration_without_activation_is_not_selected(
    registration_integration,
):
    setup = registration_integration
    approve(setup)
    complete_registration_integration(setup)
    _, lookup = services(setup)

    assert lookup.resolve(table=setup["table"]) is None


def test_lookup_verifies_current_approved_contract(
    registration_integration,
):
    setup = registration_integration
    activation, lookup = services(setup)
    activated = activate_initial(setup, activation)

    resolved = lookup.resolve(table=setup["table"])

    assert resolved["selection"] == activated["selection"]
    assert resolved["approval_revision"] == 2
    assert resolved["selection"]["registered_contract"][
        "contract_version"
    ] == "v1"

    assert setup["invocation"].calls == 1


def test_lookup_returns_newly_activated_contract(
    registration_integration,
):
    setup = registration_integration
    activation, lookup = services(setup)
    first = activate_initial(setup, activation)

    prepare_approved_contract_change(
        setup,
        request_id="request-two",
        contract_version="v2",
        baseline_selection=first["selection"],
    )
    second = activation.activate(
        table=setup["table"],
        request_id="request-two",
        contract_version="v2",
    )

    resolved = lookup.resolve(table=setup["table"])

    assert resolved["selection"] == second["selection"]
    assert resolved["selection"]["revision"] == 2


def test_missing_activation_evidence_blocks_lookup(
    registration_integration,
):
    setup = registration_integration
    activation, lookup = services(setup)
    activate_initial(setup, activation)

    registry = setup["registry"]
    registry.client.delete_item(
        TableName=registry.table_name,
        Key=registry._encode(
            {
                "pk": setup["request"]["pk"],
                "sk": "ACTIVATION",
            }
        ),
    )

    with pytest.raises(
        ContractLookupBlocked,
        match="activation",
    ):
        lookup.resolve(table=setup["table"])


def test_changed_registration_content_blocks_lookup(
    registration_integration,
):
    setup = registration_integration
    activation, lookup = services(setup)
    activated = activate_initial(setup, activation)
    registered = activated["selection"]["registered_contract"]

    setup["registration_table"].update_item(
        Key={
            "contract_id": registered["contract_id"],
            "contract_version": "v1",
        },
        UpdateExpression="SET schema_fingerprint = :changed",
        ExpressionAttributeValues={":changed": "f" * 64},
    )

    with pytest.raises(
        ContractVerificationError,
        match="fingerprint",
    ):
        lookup.resolve(table=setup["table"])


def test_selection_change_during_lookup_is_rejected(
    registration_integration, monkeypatch
):
    setup = registration_integration
    activation, lookup = services(setup)
    activated = activate_initial(setup, activation)

    registry = setup["registry"]
    original_read = registry._read
    selection_reads = 0

    def read_with_concurrent_change(partition_key, sort_key):
        nonlocal selection_reads

        if (
            partition_key == setup["table"].partition_key
            and sort_key == "CONTRACT_CURRENT"
        ):
            selection_reads += 1

            if selection_reads == 2:
                changed = copy.deepcopy(activated["selection"])
                changed["revision"] = 2
                return changed

        return original_read(partition_key, sort_key)

    monkeypatch.setattr(
        registry, "_read", read_with_concurrent_change
    )

    with pytest.raises(
        ContractLookupBlocked,
        match="changed during lookup",
    ):
        lookup.resolve(table=setup["table"])