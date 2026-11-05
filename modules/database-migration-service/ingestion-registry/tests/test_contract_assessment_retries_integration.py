import hashlib
import json
from dataclasses import asdict
from unittest.mock import Mock

import pytest

from ingestion_registry.contract_assessment import (
    ContractAssessmentAdmission,
)
from ingestion_registry.contract_candidate import ContractCandidateReader
from ingestion_registry.contract_lifecycle import ContractRequestConflict
from ingestion_registry.contract_policy import ContractPolicy
from ingestion_registry.references import S3ObjectReference


@pytest.fixture
def assessment_retry(registration_integration):
    setup = registration_integration
    table = setup["table"]
    bucket = setup["candidate"].bucket_name
    request_id = "assessment-retry-request"

    lookup = Mock()
    lookup.resolve.return_value = None

    reader = ContractCandidateReader(
        s3_client=setup["s3"],
        candidate_bucket=bucket,
        candidate_prefix="candidates/",
        bucket_owner_account_id="123456789012",
    )

    admission = ContractAssessmentAdmission(
        lifecycle=setup["lifecycle"],
        candidate_reader=reader,
        current_lookup=lookup,
        s3_client=setup["s3"],
        evidence_bucket=bucket,
        evidence_prefix="validation/",
        bucket_owner_account_id="123456789012",
        assessor_revision="test-assessor-v1",
    )

    document = {
        "validation_result_version": 1,
        "request_id": request_id,
        "identity": {
            "source": table.source,
            "database_name": table.database_name,
            "schema_name": table.schema_name,
            "table_name": table.table_name,
            "contract_version": "v1",
        },
        "candidate_reference": asdict(setup["candidate"]),
        "baseline_reference": None,
        "assessor_revision": "test-assessor-v1",
        "validation": "VALID",
        "compatibility": "FIRST_CONTRACT",
    }

    raw = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")

    key = "validation/assessment-retry.json"

    def publish_evidence():
        response = setup["s3"].put_object(
            Bucket=bucket,
            Key=key,
            Body=raw,
        )

        return S3ObjectReference(
            bucket_name=bucket,
            object_key=key,
            version_id=response["VersionId"],
            sha256=hashlib.sha256(raw).hexdigest(),
        )

    evidence = publish_evidence()

    arguments = {
        "table": table,
        "request_id": request_id,
        "contract_version": "v1",
        "candidate_reference": setup["candidate"],
        "evidence_reference": evidence,
        "policy": ContractPolicy(revision="policy-v1"),
    }

    return {
        **setup,
        "admission": admission,
        "lookup": lookup,
        "arguments": arguments,
        "publish_evidence": publish_evidence,
    }


def test_retry_keeps_original_baseline_after_current_changes(
    assessment_retry,
):
    setup = assessment_retry
    original = setup["admission"].submit(**setup["arguments"])

    setup["lookup"].resolve.reset_mock()
    setup["lookup"].resolve.side_effect = AssertionError(
        "A duplicate assessment must not select a new baseline"
    )

    repeated = setup["admission"].submit(**setup["arguments"])

    assert repeated == original
    setup["lookup"].resolve.assert_not_called()


def test_retry_rejects_changed_policy(
    assessment_retry,
):
    setup = assessment_retry
    setup["admission"].submit(**setup["arguments"])

    changed = {
        **setup["arguments"],
        "policy": ContractPolicy(revision="policy-v2"),
    }

    with pytest.raises(ContractRequestConflict):
        setup["admission"].submit(**changed)


def test_retry_rejects_different_evidence_object_version(
    assessment_retry,
):
    setup = assessment_retry
    setup["admission"].submit(**setup["arguments"])

    replacement = setup["publish_evidence"]()

    assert replacement.version_id != (
        setup["arguments"]["evidence_reference"].version_id
    )

    changed = {
        **setup["arguments"],
        "evidence_reference": replacement,
    }

    with pytest.raises(ContractRequestConflict):
        setup["admission"].submit(**changed)