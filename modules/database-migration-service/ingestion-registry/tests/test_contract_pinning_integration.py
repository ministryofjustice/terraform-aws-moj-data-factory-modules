import pytest

from contract_integration_support import (
    approve,
    prepare_approved_contract_change,
)
from ingestion_registry.contract_activation import ContractActivation
from ingestion_registry.contract_lookup import CurrentContractLookup
from ingestion_registry.contract_pinning import CurrentContractPinning
from ingestion_registry.contracts import IngestionContracts
from ingestion_registry.ownership import IngestionOwnership, OwnershipLost
from ingestion_registry.progress import IngestionProgress, ProgressConflict
from ingestion_registry.requests import NewIngestionRequest


def pinning_setup(setup):
    registry = setup["registry"]
    activation = ContractActivation(
        completion=setup["completion"],
        lifecycle=setup["lifecycle"],
    )
    approve(setup)
    activated = activation.activate(
        table=setup["table"],
        request_id="request-one",
        contract_version="v1",
    )

    admitted = registry.admit(
        NewIngestionRequest(
            request_id="ingestion-request-one",
            operation="INITIAL_INGESTION",
            table=setup["table"],
            configuration_revision="a" * 64,
            artifact_revision="b" * 64,
            extraction_generation="generation-one",
        )
    )
    clock = {"now": 1000}
    ownership = IngestionOwnership(
        registry,
        clock=lambda: clock["now"],
    )
    claim = ownership.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="execution-one",
    )
    progress = IngestionProgress(
        registry,
        clock=lambda: clock["now"],
    )
    registrar = setup["completion"].registrar
    lookup = CurrentContractLookup(
        registry=registry,
        approval_gate=registrar.approval_gate,
        verifier=registrar.verifier,
    )
    pinning = CurrentContractPinning(
        lookup=lookup,
        contracts=IngestionContracts(progress),
    )

    return {
        "activation": activation,
        "selection": activated["selection"],
        "admitted": admitted,
        "clock": clock,
        "claim": claim,
        "progress": progress,
        "lookup": lookup,
        "pinning": pinning,
    }


def activate_second(setup, context):
    prepare_approved_contract_change(
        setup,
        request_id="request-two",
        contract_version="v2",
        baseline_selection=context["selection"],
    )
    return context["activation"].activate(
        table=setup["table"],
        request_id="request-two",
        contract_version="v2",
    )


def pin(context):
    return context["pinning"].pin(
        context["claim"],
        update_id="select-contract",
        expected_revision=0,
    )


def test_pins_verified_current_contract(registration_integration):
    setup = registration_integration
    context = pinning_setup(setup)

    result = pin(context)

    assert result["status"] == "SUCCEEDED"
    assert result["result_reference"]["contract_version"] == "v1"
    assert result["result_reference"]["contract_object_version_id"] == (
        context["selection"]["registered_contract"][
            "object_reference"
        ]["version_id"]
    )


def test_unpinned_ingestion_selects_new_current_contract(
    registration_integration,
):
    setup = registration_integration
    context = pinning_setup(setup)
    second = activate_second(setup, context)

    result = pin(context)

    assert result["result_reference"]["contract_version"] == "v2"
    assert result["result_reference"]["contract_object_version_id"] == (
        second["selection"]["registered_contract"][
            "object_reference"
        ]["version_id"]
    )


def test_retry_keeps_original_pin_after_new_activation(
    registration_integration, monkeypatch
):
    setup = registration_integration
    context = pinning_setup(setup)
    original = pin(context)
    activate_second(setup, context)

    def unexpected_lookup(**kwargs):
        raise AssertionError("Completed pin must not resolve current contract")

    monkeypatch.setattr(
        context["lookup"], "resolve", unexpected_lookup
    )

    retried = context["pinning"].pin(
        context["claim"],
        update_id="another-delivery",
        expected_revision=1,
    )

    assert retried == original
    assert retried["result_reference"]["contract_version"] == "v1"


def test_selection_change_between_lookup_and_pin_is_blocked(
    registration_integration, monkeypatch
):
    setup = registration_integration
    context = pinning_setup(setup)
    original_resolve = context["lookup"].resolve

    def resolve_then_activate(**kwargs):
        resolved = original_resolve(**kwargs)
        activate_second(setup, context)
        return resolved

    monkeypatch.setattr(
        context["lookup"], "resolve", resolve_then_activate
    )

    with pytest.raises(ProgressConflict):
        pin(context)

    assert context["progress"].read_stage(
        ingestion_id=context["admitted"].ingestion_id,
        stage="CONTRACT",
    ) is None


def test_completed_pin_retry_still_requires_current_ownership(
    registration_integration,
):
    setup = registration_integration
    context = pinning_setup(setup)
    pin(context)

    context["clock"]["now"] = context["claim"].lease_expires_at

    with pytest.raises(OwnershipLost):
        pin(context)