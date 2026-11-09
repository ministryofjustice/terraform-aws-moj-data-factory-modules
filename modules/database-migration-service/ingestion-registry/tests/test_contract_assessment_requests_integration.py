import pytest

from ingestion_registry.contract_assessment_requests import (
    ContractAssessmentRequests,
    ContractAssessmentRequestBlocked,
)

from test_contract_orchestration_generation_integration import (
    connected_orchestration as connected_orchestration,
    prepare,
    evidence,
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


@pytest.fixture
def assessment_requests(connected_orchestration):
    setup = connected_orchestration

    service = ContractAssessmentRequests(
        generation_completion=setup["completion"],
        current_lookup=setup["coordinator"].lookup,
        assessor_revision="test-assessor-v1",
    )

    return {**setup, "assessment_requests": service}


def request_assessment(setup, prepared):
    return setup["assessment_requests"].prepare(
        table=setup["table"],
        reservation_key=prepared["reservation_key"],
        preparation_fingerprint=prepared["preparation_fingerprint"],
    )


def activate_prepared_contract(setup, prepared):
    reference = evidence(setup, prepared)
    complete_assessment(setup, prepared, reference)

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

    return complete_assessment(setup, prepared, reference)


def test_first_assessment_records_absent_baseline(assessment_requests):
    setup = assessment_requests
    prepared = prepare(setup)

    result = request_assessment(setup, prepared)

    assert result["request"]["baseline_reference"] is None
    assert result["request"]["candidate_reference"] == (
        prepared["candidate_reference"]
    )
    assert result["request"]["identity"]["contract_version"] == (
        prepared["contract_version"]
    )
    assert result["request"]["assessor_revision"] == "test-assessor-v1"

    assert setup["registry"]._read(
        prepared["reservation_key"],
        "ASSESSMENT_REQUEST",
    ) == result


def test_retry_retains_original_baseline_after_activation(
    assessment_requests,
):
    setup = assessment_requests
    prepared = prepare(setup)

    original = request_assessment(setup, prepared)
    activate_prepared_contract(setup, prepared)

    repeated = request_assessment(setup, prepared)

    assert repeated == original
    assert repeated["request"]["baseline_reference"] is None


def test_new_assessment_pins_existing_approved_baseline(
    assessment_requests,
):
    setup = assessment_requests
    first = prepare(setup)
    ready = activate_prepared_contract(setup, first)

    next_candidate = setup["coordinator"].prepare(
        table=setup["table"],
        request_id="next-assessment-candidate",
    )

    result = request_assessment(setup, next_candidate)

    assert result["baseline_selection"] == ready["selection"]
    assert result["request"]["baseline_reference"] == (
        ready["selection"]["registered_contract"]["object_reference"]
    )
    assert result["request"]["identity"]["contract_version"] == (
        next_candidate["contract_version"]
    )


def test_concurrent_baseline_change_blocks_request_write(
    assessment_requests,
    monkeypatch,
):
    setup = assessment_requests
    prepared = prepare(setup)
    lookup = setup["coordinator"].lookup
    original_resolve = lookup.resolve

    def resolve_then_activate(**kwargs):
        resolved = original_resolve(**kwargs)

        monkeypatch.setattr(
            lookup,
            "resolve",
            original_resolve,
        )

        activate_prepared_contract(setup, prepared)
        return resolved

    monkeypatch.setattr(
        lookup,
        "resolve",
        resolve_then_activate,
    )

    with pytest.raises(
        ContractAssessmentRequestBlocked,
        match="changed",
    ):
        request_assessment(setup, prepared)

    assert setup["registry"]._read(
        prepared["reservation_key"],
        "ASSESSMENT_REQUEST",
    ) is None