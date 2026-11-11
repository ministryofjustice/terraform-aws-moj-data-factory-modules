import copy
import hashlib
import json
from unittest.mock import Mock

import pytest

from ingestion_registry.contract_assessment import (
    ContractAssessmentAdmission,
    ContractAssessmentBlocked,
)
from ingestion_registry.contract_generation_assessment import (
    GeneratedContractAssessment,
    GeneratedContractAssessmentBlocked,
)
from ingestion_registry.contract_lifecycle import ContractRequestConflict
from ingestion_registry.contract_policy import ContractPolicy
from ingestion_registry.references import S3ObjectReference

from test_contract_generation_completion_integration import (
    generation_completion as generation_completion,
)
from test_candidate_generation_integration import (
    candidate_generation as candidate_generation,
    generation_adapter as generation_adapter,
)


@pytest.fixture
def generated_assessment(generation_completion):
    setup = generation_completion
    reservation = setup["reservation"]

    generated = setup["completion"].complete(
        table=setup["table"],
        reservation_key=reservation["pk"],
        request_fingerprint=reservation["request_fingerprint"],
    )

    lookup = Mock()
    lookup.resolve.return_value = None

    admission = ContractAssessmentAdmission(
        lifecycle=setup["lifecycle"],
        candidate_reader=setup["candidate_reader"],
        current_lookup=lookup,
        s3_client=setup["s3"],
        evidence_bucket=setup["candidate"].bucket_name,
        evidence_prefix="validation/",
        bucket_owner_account_id="123456789012",
        assessor_revision="test-assessor-v1",
    )

    service = GeneratedContractAssessment(
        generation_completion=setup["completion"],
        assessment_admission=admission,
    )

    document = {
        "validation_result_version": 1,
        "request_id": generated["request_id"],
        "identity": {
            field: generated[field]
            for field in (
                "source",
                "database_name",
                "schema_name",
                "table_name",
                "contract_version",
            )
        },
        "candidate_reference": generated["candidate_reference"],
        "baseline_reference": None,
        "assessor_revision": "test-assessor-v1",
        "validation": "VALID",
        "compatibility": "FIRST_CONTRACT",
    }

    def publish_evidence(value=None):
        raw = json.dumps(
            document if value is None else value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")

        bucket = setup["candidate"].bucket_name
        key = "validation/generated-contract/result.json"

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

    setup["invocation"].invoke.reset_mock()
    setup["secret_lookup"].reset_mock()

    return {
        **setup,
        "service": service,
        "generated": generated,
        "document": document,
        "evidence": evidence,
        "publish_evidence": publish_evidence,
        "lookup": lookup,
    }


def submit(setup, **changes):
    arguments = {
        "table": setup["table"],
        "reservation_key": setup["reservation"]["pk"],
        "request_fingerprint": setup["reservation"][
            "request_fingerprint"
        ],
        "evidence_reference": setup["evidence"],
        "policy": ContractPolicy(revision="policy-v1"),
    }
    arguments.update(changes)

    return setup["service"].submit(**arguments)


def test_assessment_uses_the_recorded_generated_candidate(
    generated_assessment,
):
    setup = generated_assessment

    result = submit(setup)

    assert result["status"] == "PENDING_REVIEW"
    assert result["request_id"] == (
        setup["generated"]["request_id"]
    )
    assert result["assessment"]["candidate_reference"] == (
        setup["generated"]["candidate_reference"]
    )

    setup["invocation"].invoke.assert_not_called()
    setup["secret_lookup"].assert_not_called()

    assert setup["registration_table"].scan(
        ConsistentRead=True
    )["Count"] == 0

    assert setup["registry"]._read(
        setup["table"].partition_key,
        "CONTRACT_CURRENT",
    ) is None


def test_duplicate_assessment_reuses_original_request(
    generated_assessment,
):
    setup = generated_assessment

    original = submit(setup)

    setup["lookup"].resolve.reset_mock()
    setup["lookup"].resolve.side_effect = AssertionError(
        "Retry must retain the recorded baseline"
    )

    assert submit(setup) == original

    setup["lookup"].resolve.assert_not_called()
    setup["invocation"].invoke.assert_not_called()
    setup["secret_lookup"].assert_not_called()


@pytest.mark.parametrize(
    "change",
    ["candidate", "version", "request"],
)
def test_evidence_must_match_recorded_generation(
    generated_assessment,
    change,
):
    setup = generated_assessment
    document = copy.deepcopy(setup["document"])

    if change == "candidate":
        document["candidate_reference"]["version_id"] = (
            "another-candidate-version"
        )
    elif change == "version":
        document["identity"]["contract_version"] = "v3"
    else:
        document["request_id"] = "another-request"

    evidence = setup["publish_evidence"](document)

    with pytest.raises(ContractAssessmentBlocked, match="exact"):
        submit(setup, evidence_reference=evidence)

    assert setup["lifecycle"].read(
        table=setup["table"],
        request_id=setup["generated"]["request_id"],
    ) is None

    setup["invocation"].invoke.assert_not_called()


def test_missing_completion_does_not_start_generation(
    generated_assessment,
):
    setup = generated_assessment

    setup["registry"].client.delete_item(
        TableName=setup["registry"].table_name,
        Key=setup["registry"]._encode(
            {
                "pk": setup["reservation"]["pk"],
                "sk": "GENERATION",
            }
        ),
    )

    with pytest.raises(
        GeneratedContractAssessmentBlocked,
        match="recorded",
    ):
        submit(setup)

    setup["invocation"].invoke.assert_not_called()
    setup["secret_lookup"].assert_not_called()


def test_duplicate_request_rejects_replaced_evidence(
    generated_assessment,
):
    setup = generated_assessment
    submit(setup)

    replacement = setup["publish_evidence"]()

    assert replacement.version_id != setup["evidence"].version_id

    with pytest.raises(ContractRequestConflict):
        submit(setup, evidence_reference=replacement)

    setup["invocation"].invoke.assert_not_called()