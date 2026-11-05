import copy
import hashlib
import io
import json
from dataclasses import asdict
from unittest.mock import Mock

import pytest

from ingestion_registry.contract_assessment import (
    ContractAssessmentAdmission,
    ContractAssessmentBlocked,
)
from ingestion_registry.contract_candidate import VerifiedCandidate
from ingestion_registry.contract_policy import (
    CompatibilityOutcome,
    ContractPolicy,
    ValidationOutcome,
)
from ingestion_registry.identity import TableIdentity
from ingestion_registry.references import S3ObjectReference


@pytest.fixture
def assessment_setup():
    table = TableIdentity(
        environment="development",
        source="sandbox-postgres",
        database_name="dms_test",
        schema_name="public",
        table_name="dms_integration_test",
    )
    candidate_reference = S3ObjectReference(
        bucket_name="example-candidates",
        object_key="candidates/request-one/contract.avsc",
        version_id="candidate-one",
        sha256="a" * 64,
    )
    document = {
        "validation_result_version": 1,
        "request_id": "request-one",
        "identity": {
            "source": table.source,
            "database_name": table.database_name,
            "schema_name": table.schema_name,
            "table_name": table.table_name,
            "contract_version": "v1",
        },
        "candidate_reference": asdict(candidate_reference),
        "baseline_reference": None,
        "assessor_revision": "validator-release-one",
        "validation": "VALID",
        "compatibility": "FIRST_CONTRACT",
    }

    lifecycle = Mock()
    lifecycle.submit_assessment.return_value = {
        "status": "PENDING_REVIEW"
    }
    reader = Mock()
    reader.read.return_value = VerifiedCandidate(
        reference=candidate_reference,
        contract_version="v1",
        contract_fingerprint="b" * 64,
        content=b"candidate bytes",
    )
    lookup = Mock()
    lookup.resolve.return_value = None
    s3 = Mock()

    admission = ContractAssessmentAdmission(
        lifecycle=lifecycle,
        candidate_reader=reader,
        current_lookup=lookup,
        s3_client=s3,
        evidence_bucket="example-evidence",
        evidence_prefix="validation/",
        bucket_owner_account_id="123456789012",
        assessor_revision="validator-release-one",
    )

    return {
        "table": table,
        "candidate": candidate_reference,
        "document": document,
        "lifecycle": lifecycle,
        "reader": reader,
        "lookup": lookup,
        "s3": s3,
        "admission": admission,
    }


def submit_document(setup, document=None, checksum=None):
    document = (
        setup["document"] if document is None else document
    )
    raw = json.dumps(document).encode("utf-8")
    body = io.BytesIO(raw)
    setup["s3"].get_object.return_value = {
        "Body": body,
        "VersionId": "validation-one",
    }
    evidence = S3ObjectReference(
        bucket_name="example-evidence",
        object_key="validation/request-one/result.json",
        version_id="validation-one",
        sha256=checksum or hashlib.sha256(raw).hexdigest(),
    )

    try:
        return setup["admission"].submit(
            table=setup["table"],
            request_id="request-one",
            contract_version="v1",
            candidate_reference=setup["candidate"],
            evidence_reference=evidence,
            policy=ContractPolicy(revision="policy-v1"),
        )
    finally:
        assert body.closed


def test_verified_result_is_passed_to_lifecycle(assessment_setup):
    setup = assessment_setup

    result = submit_document(setup)

    assert result["status"] == "PENDING_REVIEW"
    arguments = setup["lifecycle"].submit_assessment.call_args.kwargs
    assessment = arguments["assessment"]
    assert assessment.validation == ValidationOutcome.VALID
    assert assessment.compatibility == (
        CompatibilityOutcome.FIRST_CONTRACT
    )
    assert assessment.candidate_reference == setup["candidate"]
    assert assessment.baseline_reference is None

    setup["s3"].get_object.assert_called_once_with(
        Bucket="example-evidence",
        Key="validation/request-one/result.json",
        VersionId="validation-one",
        ExpectedBucketOwner="123456789012",
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_id", "another-request"),
        ("assessor_revision", "another-validator"),
        ("validation_result_version", True),
    ],
)
def test_mismatched_context_is_rejected(
    assessment_setup, field, value
):
    setup = assessment_setup
    document = copy.deepcopy(setup["document"])
    document[field] = value

    with pytest.raises(ContractAssessmentBlocked, match="exact"):
        submit_document(setup, document)

    setup["lifecycle"].submit_assessment.assert_not_called()


def test_different_candidate_version_is_rejected(assessment_setup):
    setup = assessment_setup
    document = copy.deepcopy(setup["document"])
    document["candidate_reference"]["version_id"] = "candidate-two"

    with pytest.raises(ContractAssessmentBlocked, match="exact"):
        submit_document(setup, document)

    setup["lifecycle"].submit_assessment.assert_not_called()


def test_unexpected_baseline_is_rejected(assessment_setup):
    setup = assessment_setup
    document = copy.deepcopy(setup["document"])
    document["baseline_reference"] = asdict(setup["candidate"])

    with pytest.raises(ContractAssessmentBlocked, match="exact"):
        submit_document(setup, document)

    setup["lifecycle"].submit_assessment.assert_not_called()


def test_checksum_mismatch_prevents_admission(assessment_setup):
    setup = assessment_setup

    with pytest.raises(ContractAssessmentBlocked, match="checksum"):
        submit_document(setup, checksum="f" * 64)

    setup["lifecycle"].submit_assessment.assert_not_called()


def test_unknown_outcome_prevents_admission(assessment_setup):
    setup = assessment_setup
    document = copy.deepcopy(setup["document"])
    document["compatibility"] = "PROBABLY_COMPATIBLE"

    with pytest.raises(ContractAssessmentBlocked, match="outcomes"):
        submit_document(setup, document)

    setup["lifecycle"].submit_assessment.assert_not_called()


def test_invalid_contract_cannot_claim_compatibility(assessment_setup):
    setup = assessment_setup
    document = copy.deepcopy(setup["document"])
    document["validation"] = "INVALID"
    document["compatibility"] = "COMPATIBLE"

    with pytest.raises(ContractAssessmentBlocked, match="outcomes"):
        submit_document(setup, document)

    setup["lifecycle"].submit_assessment.assert_not_called()


def test_invalid_result_can_be_recorded_as_blocked(assessment_setup):
    setup = assessment_setup
    document = copy.deepcopy(setup["document"])
    document["validation"] = "INVALID"
    document["compatibility"] = "UNSUPPORTED"
    setup["lifecycle"].submit_assessment.return_value = {
        "status": "BLOCKED"
    }

    result = submit_document(setup, document)

    assert result["status"] == "BLOCKED"
    assessment = setup["lifecycle"].submit_assessment.call_args.kwargs[
        "assessment"
    ]
    assert assessment.validation == ValidationOutcome.INVALID