import pytest

from ingestion_registry.contract_pinning import CurrentContractPinning
from ingestion_registry.contracts import IngestionContracts
from ingestion_registry.ownership import IngestionOwnership
from ingestion_registry.progress import IngestionProgress, ProgressConflict
from ingestion_registry.requests import NewIngestionRequest

from test_contract_policy_lookup_integration import (
    policy_lookup as policy_lookup,
)
from test_contract_policy_activation_integration import (
    policy_activation as policy_activation,
    activate_later_human_approved_contract,
)
from test_contract_policy_registration_completion_integration import (
    policy_completion as policy_completion,
)
from test_contract_policy_authorisation_integration import (
    policy_authorisation_setup as policy_authorisation_setup,
)
from test_contract_reuse_integration import (
    reuse_setup as reuse_setup,
)


@pytest.fixture
def policy_pinning(policy_lookup):
    setup = policy_lookup
    registry = setup["registry"]

    admitted = registry.admit(
        NewIngestionRequest(
            request_id="policy-ingestion-request",
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
        execution_id="policy-ingestion-execution",
    )

    progress = IngestionProgress(
        registry,
        clock=lambda: clock["now"],
    )
    contracts = IngestionContracts(progress)

    pinning = CurrentContractPinning(
        lookup=setup["policy_lookup"],
        contracts=contracts,
    )

    return {
        **setup,
        "admitted": admitted,
        "clock": clock,
        "ownership": ownership,
        "claim": claim,
        "progress": progress,
        "contracts": contracts,
        "pinning": pinning,
    }


def pin(setup, claim=None):
    return setup["pinning"].pin(
        setup["claim"] if claim is None else claim,
        update_id="policy-contract-selected",
        expected_revision=0,
    )


def recorded_stage(setup):
    return setup["progress"].read_stage(
        ingestion_id=setup["admitted"].ingestion_id,
        stage="CONTRACT",
    )


def test_current_policy_contract_is_pinned(policy_pinning):
    setup = policy_pinning

    pin(setup)

    stage = recorded_stage(setup)
    reference = stage["result_reference"]

    assert stage["status"] == "SUCCEEDED"
    assert reference["contract_version"] == "v2"
    assert reference["authorisation_kind"] == "POLICY"
    assert reference["policy_revision"] == "compatible-policy-v1"
    assert reference["authorisation_request_key"] == (
        setup["policy_request"]["pk"]
    )
    assert reference["authorisation_object_version_id"] == (
        setup["policy_evidence"].version_id
    )
    assert not any(
        field.startswith("approval_")
        for field in reference
    )


def test_completed_pin_does_not_resolve_current_policy_again(
    policy_pinning,
    monkeypatch,
):
    setup = policy_pinning
    original = pin(setup)

    def unexpected_lookup(**kwargs):
        raise AssertionError(
            "Completed pin must reuse its recorded selection"
        )

    monkeypatch.setattr(
        setup["policy_lookup"],
        "resolve",
        unexpected_lookup,
    )

    assert pin(setup) == original


def test_recovery_keeps_original_policy_contract(policy_pinning):
    setup = policy_pinning
    original = pin(setup)

    activate_later_human_approved_contract(
        setup,
        setup["activated"]["selection"],
    )

    setup["clock"]["now"] = setup["claim"].lease_expires_at

    recovered = setup["ownership"].claim(
        ingestion_id=setup["admitted"].ingestion_id,
        execution_id="recovered-policy-ingestion",
    )

    assert pin(setup, recovered) == original

    reference = recorded_stage(setup)["result_reference"]
    assert reference["contract_version"] == "v2"
    assert reference["authorisation_kind"] == "POLICY"


def test_current_selection_change_before_pin_blocks_write(
    policy_pinning,
    monkeypatch,
):
    setup = policy_pinning
    lookup = setup["policy_lookup"]
    original_resolve = lookup.resolve

    def resolve_then_activate_new_contract(**kwargs):
        resolved = original_resolve(**kwargs)

        activate_later_human_approved_contract(
            setup,
            setup["activated"]["selection"],
        )

        return resolved

    monkeypatch.setattr(
        lookup,
        "resolve",
        resolve_then_activate_new_contract,
    )

    with pytest.raises(ProgressConflict):
        pin(setup)

    assert recorded_stage(setup) is None


def test_policy_decision_change_before_pin_blocks_write(
    policy_pinning,
    monkeypatch,
):
    setup = policy_pinning
    original_record_stage = setup["progress"].record_stage

    def change_decision_then_record(*args, **kwargs):
        request = setup["lifecycle"].read(
            table=setup["table"],
            request_id="policy-request",
        )
        request["status"] = "BLOCKED"

        setup["registry"].client.put_item(
            TableName=setup["registry"].table_name,
            Item=setup["registry"]._encode(request),
        )

        return original_record_stage(*args, **kwargs)

    monkeypatch.setattr(
        setup["progress"],
        "record_stage",
        change_decision_then_record,
    )

    with pytest.raises(ProgressConflict):
        pin(setup)

    assert recorded_stage(setup) is None