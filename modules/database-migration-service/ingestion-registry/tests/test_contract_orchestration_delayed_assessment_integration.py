import pytest

from contract_integration_support import encode, store_reference
from ingestion_registry.contract_activation import (
    ContractActivationConflict,
)

from test_contract_orchestration_generation_integration import (
    connected_orchestration as connected_orchestration,
    complete_assessment,
)
from test_contract_preparation_integration import (
    preparation_setup as preparation_setup,
)
from test_contract_generation_completion_integration import (
    generation_completion as generation_completion,
)
from test_candidate_generation_integration import (
    candidate_generation as candidate_generation,
    generation_adapter as generation_adapter,
)
from test_contract_orchestration_integration import approve_request


def prepare_candidate(setup, request_id):
    return setup["coordinator"].prepare(
        table=setup["table"],
        request_id=request_id,
    )


def assessment_evidence(setup, prepared):
    recorded = setup["coordinator"].request_assessment(
        table=setup["table"],
        reservation_key=prepared["reservation_key"],
        preparation_fingerprint=prepared["preparation_fingerprint"],
    )

    document = {
        key: value
        for key, value in recorded["request"].items()
        if key != "assessment_request_version"
    }

    document["validation_result_version"] = 1
    document["validation"] = "VALID"
    document["compatibility"] = (
        "FIRST_CONTRACT"
        if document["baseline_reference"] is None
        else "COMPATIBLE"
    )

    reference = store_reference(
        setup["s3"],
        "test-contract-evidence",
        f"validation/{prepared['request_id']}/result.json",
        encode(document),
    )

    return reference, recorded


def approve_prepared(setup, prepared):
    request = setup["lifecycle"].read(
        table=setup["table"],
        request_id=prepared["request_id"],
    )

    approve_request(
        {
            **setup,
            "orchestration_request": request,
        }
    )


def activate_candidate(setup, prepared, reference):
    waiting = complete_assessment(setup, prepared, reference)
    assert waiting["status"] == "REVIEW_REQUIRED"

    approve_prepared(setup, prepared)

    ready = complete_assessment(setup, prepared, reference)
    assert ready["status"] == "CONTRACT_READY"

    return ready["selection"]


def assert_not_activated(setup, prepared, current_selection):
    request = setup["lifecycle"].read(
        table=setup["table"],
        request_id=prepared["request_id"],
    )

    assert setup["registry"]._read(
        request["pk"],
        "ACTIVATION",
    ) is None

    assert setup["registry"]._read(
        setup["table"].partition_key,
        "CONTRACT_CURRENT",
    ) == current_selection


def test_delayed_first_contract_cannot_replace_new_current_contract(
    connected_orchestration,
):
    setup = connected_orchestration

    delayed = prepare_candidate(
        setup,
        "delayed-first-contract",
    )
    delayed_evidence, recorded = assessment_evidence(setup, delayed)

    assert recorded["request"]["baseline_reference"] is None

    competing = prepare_candidate(
        setup,
        "competing-first-contract",
    )
    competing_evidence, _ = assessment_evidence(setup, competing)

    newer_selection = activate_candidate(
        setup,
        competing,
        competing_evidence,
    )

    waiting = complete_assessment(
        setup,
        delayed,
        delayed_evidence,
    )

    assert waiting["status"] == "REVIEW_REQUIRED"

    request = setup["lifecycle"].read(
        table=setup["table"],
        request_id=delayed["request_id"],
    )
    assert request["assessment"]["baseline_reference"] is None

    approve_prepared(setup, delayed)

    with pytest.raises(
        ContractActivationConflict,
        match="current contract already exists",
    ):
        complete_assessment(setup, delayed, delayed_evidence)

    assert_not_activated(setup, delayed, newer_selection)


def test_delayed_change_retains_original_baseline_but_cannot_activate(
    connected_orchestration,
):
    setup = connected_orchestration

    initial = prepare_candidate(
        setup,
        "initial-approved-contract",
    )
    initial_evidence, _ = assessment_evidence(setup, initial)

    original_selection = activate_candidate(
        setup,
        initial,
        initial_evidence,
    )
    original_baseline = original_selection[
        "registered_contract"
    ]["object_reference"]

    delayed = prepare_candidate(
        setup,
        "delayed-compatible-change",
    )
    delayed_evidence, recorded = assessment_evidence(setup, delayed)

    assert recorded["request"]["baseline_reference"] == original_baseline

    competing = prepare_candidate(
        setup,
        "competing-compatible-change",
    )
    competing_evidence, _ = assessment_evidence(setup, competing)

    newer_selection = activate_candidate(
        setup,
        competing,
        competing_evidence,
    )

    generation_calls = setup["invocation"].invoke.call_count
    database_calls = setup["secret_lookup"].call_count

    waiting = complete_assessment(
        setup,
        delayed,
        delayed_evidence,
    )

    assert waiting["status"] == "REVIEW_REQUIRED"
    assert waiting["reason"] == "COMPATIBLE_CHANGE_REQUIRES_APPROVAL"

    request = setup["lifecycle"].read(
        table=setup["table"],
        request_id=delayed["request_id"],
    )
    assert request["assessment"]["baseline_reference"] == original_baseline

    assert complete_assessment(
        setup,
        delayed,
        delayed_evidence,
    ) == waiting

    approve_prepared(setup, delayed)

    with pytest.raises(
        ContractActivationConflict,
        match="baseline is no longer current",
    ):
        complete_assessment(setup, delayed, delayed_evidence)

    assert_not_activated(setup, delayed, newer_selection)

    assert setup["invocation"].invoke.call_count == generation_calls
    assert setup["secret_lookup"].call_count == database_calls