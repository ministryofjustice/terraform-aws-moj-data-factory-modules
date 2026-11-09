import copy
from dataclasses import asdict

import pytest

from contract_integration_support import encode, store_reference
from ingestion_registry.contract_activation import ContractActivation
from ingestion_registry.contract_assessment import (
    ContractAssessmentAdmission,
    ContractAssessmentBlocked,
)
from ingestion_registry.contract_lookup import CurrentContractLookup
from ingestion_registry.contract_orchestration import (
    ContractOrchestration,
    ContractOrchestrationBlocked,
)
from ingestion_registry.contract_policy import ContractPolicy
from ingestion_registry.contract_reuse import ContractReuse
from ingestion_registry.contract_verification import RegisteredContractVerifier


@pytest.fixture
def orchestration_setup(registration_integration):
    setup = registration_integration
    policy = ContractPolicy(revision="policy-v1")

    verifier = RegisteredContractVerifier(
        contract_table=setup["registration_table"],
        s3_client=setup["s3"],
        bucket_name="test-approved-contracts",
        bucket_owner_account_id="123456789012",
    )

    lookup = CurrentContractLookup(
        registry=setup["registry"],
        approval_gate=setup["promoter"].approval_gate,
        verifier=verifier,
    )

    admission = ContractAssessmentAdmission(
        lifecycle=setup["lifecycle"],
        candidate_reader=setup["promoter"].candidate_reader,
        current_lookup=lookup,
        s3_client=setup["s3"],
        evidence_bucket="test-contract-evidence",
        evidence_prefix="validation/",
        bucket_owner_account_id="123456789012",
        assessor_revision="test-assessor-v1",
    )

    reuse = ContractReuse(
        lifecycle=setup["lifecycle"],
        current_lookup=lookup,
        contract_verifier=verifier,
    )

    activation = ContractActivation(
        completion=setup["completion"],
        lifecycle=setup["lifecycle"],
    )

    coordinator = ContractOrchestration(
        lifecycle=setup["lifecycle"],
        lookup=lookup,
        assessment_admission=admission,
        reuse=reuse,
        activation=activation,
        trusted_policy=policy,
    )

    document = {
        "validation_result_version": 1,
        "request_id": "orchestration-request",
        "identity": {
            field: setup["contract"][field]
            for field in (
                "source",
                "database_name",
                "schema_name",
                "table_name",
                "contract_version",
            )
        },
        "candidate_reference": asdict(setup["candidate"]),
        "baseline_reference": None,
        "assessor_revision": "test-assessor-v1",
        "validation": "VALID",
        "compatibility": "FIRST_CONTRACT",
    }

    evidence = store_reference(
        setup["s3"],
        "test-contract-evidence",
        "validation/orchestration-request/result.json",
        encode(document),
    )

    request = admission.submit(
        table=setup["table"],
        request_id="orchestration-request",
        contract_version="v1",
        candidate_reference=setup["candidate"],
        evidence_reference=evidence,
        policy=policy,
    )

    return {
        **setup,
        "coordinator": coordinator,
        "orchestration_request": request,
    }


def advance(setup, **changes):
    arguments = {
        "table": setup["table"],
        "request_id": "orchestration-request",
        "contract_version": "v1",
        "expected_request_fingerprint": setup[
            "orchestration_request"
        ]["request_fingerprint"],
    }
    arguments.update(changes)

    return setup["coordinator"].advance(**arguments)


def approve_request(setup):
    request = setup["orchestration_request"]

    evidence = store_reference(
        setup["s3"],
        "test-contract-evidence",
        "approvals/orchestration-request/review.json",
        encode(
            {
                "approval_version": 1,
                "table_identity": request["table_identity"],
                "request_id": request["request_id"],
                "request_fingerprint": request["request_fingerprint"],
                "review_id": "orchestration-review",
                "reviewer": "test-authorised-reviewer",
                "outcome": "APPROVED",
                "assessment": copy.deepcopy(request["assessment"]),
                "policy": copy.deepcopy(request["policy"]),
            }
        ),
    )

    setup["lifecycle"].record_review(
        table=setup["table"],
        request_id=request["request_id"],
        review_id="orchestration-review",
        reviewer="test-authorised-reviewer",
        outcome="APPROVED",
        expected_revision=1,
        expected_request_fingerprint=request["request_fingerprint"],
        evidence_reference=evidence,
        authorise=setup["authorise"],
    )


def test_missing_contract_requires_contract_process(orchestration_setup):
    setup = orchestration_setup

    result = setup["coordinator"].resolve(table=setup["table"])

    assert result["status"] == "CONTRACT_REQUIRED"
    assert setup["invocation"].calls == 0


def test_pending_review_does_not_register_contract(orchestration_setup):
    setup = orchestration_setup

    result = advance(setup)

    assert result["status"] == "REVIEW_REQUIRED"
    assert result["reason"] == "FIRST_CONTRACT_REQUIRES_APPROVAL"
    assert setup["invocation"].calls == 0
    assert setup["registration_table"].scan(
        ConsistentRead=True
    )["Count"] == 0


def test_approved_request_registers_activates_and_resolves(
    orchestration_setup,
):
    setup = orchestration_setup
    approve_request(setup)

    result = advance(setup)

    assert result["status"] == "CONTRACT_READY"
    assert result["action"] == "ACTIVATED"
    assert result["selection"]["registered_contract"][
        "contract_version"
    ] == "v1"

    calls_after_first = setup["invocation"].calls

    assert advance(setup) == result
    assert setup["invocation"].calls == calls_after_first

    resolved = setup["coordinator"].resolve(table=setup["table"])

    assert resolved["status"] == "CONTRACT_READY"
    assert resolved["action"] == "RESOLVED"
    assert resolved["selection"] == result["selection"]
    assert resolved["s3_contract_uri"] == result["s3_contract_uri"]


def test_wrong_request_fingerprint_cannot_progress(orchestration_setup):
    setup = orchestration_setup

    with pytest.raises(
        ContractOrchestrationBlocked,
        match="exact contract request",
    ):
        advance(setup, expected_request_fingerprint="f" * 64)

    assert setup["invocation"].calls == 0


def test_wrong_candidate_version_cannot_progress(orchestration_setup):
    setup = orchestration_setup

    with pytest.raises(ValueError, match="contract_version"):
        advance(setup, contract_version="v2")

    assert setup["invocation"].calls == 0