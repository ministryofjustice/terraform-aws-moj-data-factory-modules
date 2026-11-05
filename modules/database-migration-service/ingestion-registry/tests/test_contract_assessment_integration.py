import copy
from dataclasses import asdict

import pytest

from contract_integration_support import (
    approve,
    encode,
    store_reference,
)
from ingestion_registry.contract_activation import ContractActivation
from ingestion_registry.contract_assessment import (
    ContractAssessmentAdmission,
    ContractAssessmentBlocked,
)
from ingestion_registry.contract_lifecycle import ContractRequestConflict
from ingestion_registry.contract_lookup import CurrentContractLookup
from ingestion_registry.contract_policy import ContractPolicy


@pytest.fixture
def assessment_integration(registration_integration):
    setup = registration_integration
    registrar = setup["completion"].registrar

    lookup = CurrentContractLookup(
        registry=setup["registry"],
        approval_gate=registrar.approval_gate,
        verifier=registrar.verifier,
    )

    admission = ContractAssessmentAdmission(
        lifecycle=setup["lifecycle"],
        candidate_reader=setup["promoter"].candidate_reader,
        current_lookup=lookup,
        s3_client=setup["s3"],
        evidence_bucket="test-contract-evidence",
        evidence_prefix="validation/",
        bucket_owner_account_id="123456789012",
        assessor_revision="validator-release-one",
    )

    return {**setup, "assessment_admission": admission}


def validation_document(setup):
    table = setup["table"]

    return {
        "validation_result_version": 1,
        "request_id": "assessed-request",
        "identity": {
            "source": table.source,
            "database_name": table.database_name,
            "schema_name": table.schema_name,
            "table_name": table.table_name,
            "contract_version": "v1",
        },
        "candidate_reference": asdict(setup["candidate"]),
        "baseline_reference": None,
        "assessor_revision": "validator-release-one",
        "validation": "VALID",
        "compatibility": "FIRST_CONTRACT",
    }


def publish_validation(setup, document):
    return store_reference(
        setup["s3"],
        "test-contract-evidence",
        "validation/assessed-request/result.json",
        encode(document),
    )


def admit_assessment(setup, evidence):
    return setup["assessment_admission"].submit(
        table=setup["table"],
        request_id="assessed-request",
        contract_version="v1",
        candidate_reference=setup["candidate"],
        evidence_reference=evidence,
        policy=ContractPolicy(revision="policy-v1"),
    )


def read_assessed_request(setup):
    return setup["lifecycle"].read(
        table=setup["table"],
        request_id="assessed-request",
    )


def test_real_evidence_creates_pending_review(assessment_integration):
    setup = assessment_integration
    evidence = publish_validation(setup, validation_document(setup))

    result = admit_assessment(setup, evidence)

    assert result["status"] == "PENDING_REVIEW"
    assert result["assessment"]["candidate_reference"] == asdict(
        setup["candidate"]
    )
    assert result["assessment"]["evidence_reference"] == asdict(evidence)
    assert read_assessed_request(setup) == result

    assert setup["s3"].list_objects_v2(
        Bucket="test-approved-contracts"
    )["KeyCount"] == 0
    assert setup["invocation"].calls == 0


def test_duplicate_delivery_returns_original_request(
    assessment_integration,
):
    setup = assessment_integration
    evidence = publish_validation(setup, validation_document(setup))

    first = admit_assessment(setup, evidence)
    second = admit_assessment(setup, evidence)

    assert second == first
    assert second["revision"] == 1


def test_changed_evidence_cannot_replace_existing_request(
    assessment_integration,
):
    setup = assessment_integration
    document = validation_document(setup)
    first_evidence = publish_validation(setup, document)
    original = admit_assessment(setup, first_evidence)

    second_evidence = publish_validation(setup, document)

    assert second_evidence.version_id != first_evidence.version_id

    with pytest.raises(ContractRequestConflict):
        admit_assessment(setup, second_evidence)

    assert read_assessed_request(setup) == original


def test_evidence_for_different_candidate_is_rejected(
    assessment_integration,
):
    setup = assessment_integration
    document = validation_document(setup)
    document["candidate_reference"]["version_id"] = "another-version"
    evidence = publish_validation(setup, document)

    with pytest.raises(ContractAssessmentBlocked, match="exact"):
        admit_assessment(setup, evidence)

    assert read_assessed_request(setup) is None


def test_first_contract_result_is_stale_after_activation(
    assessment_integration,
):
    setup = assessment_integration
    evidence = publish_validation(setup, validation_document(setup))

    approve(setup)
    activation = ContractActivation(
        completion=setup["completion"],
        lifecycle=setup["lifecycle"],
    )
    activation.activate(
        table=setup["table"],
        request_id="request-one",
        contract_version="v1",
    )

    with pytest.raises(ContractAssessmentBlocked, match="exact"):
        admit_assessment(setup, evidence)

    assert read_assessed_request(setup) is None


def test_no_change_result_records_reuse_of_exact_baseline(
    assessment_integration,
):
    setup = assessment_integration
    approve(setup)
    activation = ContractActivation(
        completion=setup["completion"],
        lifecycle=setup["lifecycle"],
    )
    activated = activation.activate(
        table=setup["table"],
        request_id="request-one",
        contract_version="v1",
    )

    document = validation_document(setup)
    document["baseline_reference"] = copy.deepcopy(
        activated["selection"]["registered_contract"][
            "object_reference"
        ]
    )
    document["compatibility"] = "NO_CHANGE"
    evidence = publish_validation(setup, document)

    calls_before = setup["invocation"].calls
    result = admit_assessment(setup, evidence)

    assert result["status"] == "REUSE_PENDING"
    assert result["assessment"]["baseline_reference"] == (
        document["baseline_reference"]
    )
    assert setup["invocation"].calls == calls_before


def test_invalid_result_records_blocked_request(
    assessment_integration,
):
    setup = assessment_integration
    document = validation_document(setup)
    document["validation"] = "INVALID"
    document["compatibility"] = "UNSUPPORTED"
    evidence = publish_validation(setup, document)

    result = admit_assessment(setup, evidence)

    assert result["status"] == "BLOCKED"
    assert read_assessed_request(setup)["status"] == "BLOCKED"
    assert setup["invocation"].calls == 0