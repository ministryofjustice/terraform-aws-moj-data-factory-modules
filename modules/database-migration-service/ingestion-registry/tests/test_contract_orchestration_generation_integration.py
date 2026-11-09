from dataclasses import asdict

import pytest

from contract_integration_support import encode, store_reference
from ingestion_registry.contract_activation import ContractActivation
from ingestion_registry.contract_assessment import (
    ContractAssessmentAdmission,
    ContractAssessmentBlocked,
)
from ingestion_registry.contract_generation_assessment import (
    GeneratedContractAssessment,
)
from ingestion_registry.contract_lookup import CurrentContractLookup
from ingestion_registry.contract_orchestration import (
    ContractOrchestration,
    ContractOrchestrationBlocked,
)
from ingestion_registry.contract_policy import ContractPolicy
from ingestion_registry.contract_reuse import ContractReuse

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
def connected_orchestration(preparation_setup):
    setup = preparation_setup
    registration = setup["registration_completion"]
    verifier = registration.registrar.verifier

    lookup = CurrentContractLookup(
        registry=setup["registry"],
        approval_gate=setup["promoter"].approval_gate,
        verifier=verifier,
    )

    admission = ContractAssessmentAdmission(
        lifecycle=setup["lifecycle"],
        candidate_reader=setup["candidate_reader"],
        current_lookup=lookup,
        s3_client=setup["s3"],
        evidence_bucket="test-contract-evidence",
        evidence_prefix="validation/",
        bucket_owner_account_id="123456789012",
        assessor_revision="test-assessor-v1",
    )

    generated_assessment = GeneratedContractAssessment(
        generation_completion=setup["completion"],
        assessment_admission=admission,
    )

    coordinator = ContractOrchestration(
        lifecycle=setup["lifecycle"],
        lookup=lookup,
        assessment_admission=admission,
        reuse=ContractReuse(
            lifecycle=setup["lifecycle"],
            current_lookup=lookup,
            contract_verifier=verifier,
        ),
        activation=ContractActivation(
            completion=registration,
            lifecycle=setup["lifecycle"],
        ),
        trusted_policy=ContractPolicy(revision="policy-v1"),
        preparation=setup["preparation"],
        generated_assessment=generated_assessment,
    )

    return {**setup, "coordinator": coordinator}


def prepare(setup):
    return setup["coordinator"].prepare(
        table=setup["table"],
        request_id="connected-contract-request",
    )


def evidence(setup, prepared, *, wrong_candidate=False):
    candidate = dict(prepared["candidate_reference"])

    if wrong_candidate:
        candidate["version_id"] = "different-candidate-version"

    identity = asdict(setup["table"])
    identity.pop("environment")
    identity["contract_version"] = prepared["contract_version"]

    return store_reference(
        setup["s3"],
        "test-contract-evidence",
        "validation/connected-contract-request/result.json",
        encode(
            {
                "validation_result_version": 1,
                "request_id": prepared["request_id"],
                "identity": identity,
                "candidate_reference": candidate,
                "baseline_reference": None,
                "assessor_revision": "test-assessor-v1",
                "validation": "VALID",
                "compatibility": "FIRST_CONTRACT",
            }
        ),
    )


def complete_assessment(setup, prepared, reference):
    return setup["coordinator"].complete_assessment(
        table=setup["table"],
        reservation_key=prepared["reservation_key"],
        preparation_fingerprint=prepared["preparation_fingerprint"],
        evidence_reference=reference,
    )


def test_coordinator_prepares_candidate_without_registering(
    connected_orchestration,
):
    setup = connected_orchestration

    prepared = prepare(setup)

    assert prepared["status"] == "CANDIDATE_READY"
    assert prepared["contract_version"] == "v3"
    assert setup["registration_table"].scan(
        ConsistentRead=True
    )["Count"] == 0

    setup["invocation"].invoke.reset_mock()
    setup["secret_lookup"].reset_mock()

    assert prepare(setup) == prepared
    setup["invocation"].invoke.assert_not_called()
    setup["secret_lookup"].assert_not_called()


def test_recorded_generation_and_evidence_produce_review_request(
    connected_orchestration,
):
    setup = connected_orchestration
    prepared = prepare(setup)
    reference = evidence(setup, prepared)

    result = complete_assessment(setup, prepared, reference)

    assert result["status"] == "REVIEW_REQUIRED"
    assert result["request_id"] == prepared["request_id"]

    assert complete_assessment(setup, prepared, reference) == result
    assert setup["registration_table"].scan(
        ConsistentRead=True
    )["Count"] == 0


def test_different_candidate_evidence_cannot_enter_lifecycle(
    connected_orchestration,
):
    setup = connected_orchestration
    prepared = prepare(setup)
    reference = evidence(
        setup,
        prepared,
        wrong_candidate=True,
    )

    with pytest.raises(ContractAssessmentBlocked, match="exact"):
        complete_assessment(setup, prepared, reference)

    assert setup["lifecycle"].read(
        table=setup["table"],
        request_id=prepared["request_id"],
    ) is None


def test_missing_generation_completion_does_not_regenerate(
    connected_orchestration,
):
    setup = connected_orchestration
    prepared = prepare(setup)
    reference = evidence(setup, prepared)

    setup["registry"].client.delete_item(
        TableName=setup["registry"].table_name,
        Key=setup["registry"]._encode(
            {
                "pk": prepared["reservation_key"],
                "sk": "GENERATION",
            }
        ),
    )

    setup["invocation"].invoke.reset_mock()
    setup["secret_lookup"].reset_mock()

    with pytest.raises(
        ContractOrchestrationBlocked,
        match="Generation completion",
    ):
        complete_assessment(setup, prepared, reference)

    setup["invocation"].invoke.assert_not_called()
    setup["secret_lookup"].assert_not_called()


def test_preparation_assessment_and_approval_produce_ready_contract(
    connected_orchestration,
):
    setup = connected_orchestration
    prepared = prepare(setup)
    reference = evidence(setup, prepared)

    waiting = complete_assessment(setup, prepared, reference)
    assert waiting["status"] == "REVIEW_REQUIRED"

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

    ready = complete_assessment(setup, prepared, reference)

    assert ready["status"] == "CONTRACT_READY"
    assert ready["action"] == "ACTIVATED"
    assert ready["selection"]["registered_contract"][
        "contract_version"
    ] == prepared["contract_version"]

    assert setup["coordinator"].resolve(
        table=setup["table"],
    )["selection"] == ready["selection"]

    assert complete_assessment(setup, prepared, reference) == ready