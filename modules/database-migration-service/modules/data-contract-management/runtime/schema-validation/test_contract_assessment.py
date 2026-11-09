import copy
import hashlib
import io
import json
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from contract_assessment import (
    ContractAssessmentError,
    ContractAssessmentService,
)
from test_main import main


OWNER = "123456789012"
KMS_KEY = (
    "arn:aws:kms:eu-west-2:123456789012:"
    "key/12345678-1234-1234-1234-123456789012"
)


def encode(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def contract(version="v1"):
    return {
        "type": "record",
        "name": "Example",
        "namespace": "example.contracts",
        "source": "sandbox-postgres",
        "database_name": "dms_test",
        "schema_name": "public",
        "table_name": "example",
        "contract_version": version,
        "fields": [
            {"name": "id", "type": "long", "key": "primary"},
        ],
    }


def validate_contract(value):
    main.validate_required_metadata(value)
    main.extract_version_number(value["contract_version"])
    main.validate_avro_schema(value)


@pytest.fixture
def assessment_setup():
    objects = {}
    writes = []

    def store(bucket, key, document):
        raw = encode(document)
        objects[(bucket, key)] = {
            "content": raw,
            "version": "object-version-one",
        }

        return {
            "bucket_name": bucket,
            "object_key": key,
            "version_id": "object-version-one",
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    def get_object(**kwargs):
        stored = objects[(kwargs["Bucket"], kwargs["Key"])]

        if "VersionId" in kwargs:
            assert kwargs["VersionId"] == stored["version"]

        assert kwargs["ExpectedBucketOwner"] == OWNER

        return {
            "Body": io.BytesIO(stored["content"]),
            "VersionId": stored["version"],
            "ServerSideEncryption": "aws:kms",
            "SSEKMSKeyId": KMS_KEY,
        }

    def put_object(**kwargs):
        assert kwargs["IfNoneMatch"] == "*"
        assert kwargs["ExpectedBucketOwner"] == OWNER
        assert kwargs["ServerSideEncryption"] == "aws:kms"
        assert kwargs["SSEKMSKeyId"] == KMS_KEY

        key = (kwargs["Bucket"], kwargs["Key"])

        if key in objects:
            raise ClientError(
                {
                    "Error": {"Code": "PreconditionFailed"},
                    "ResponseMetadata": {"HTTPStatusCode": 412},
                },
                "PutObject",
            )

        writes.append(kwargs)
        objects[key] = {
            "content": kwargs["Body"],
            "version": "evidence-version-one",
        }

        return {"VersionId": "evidence-version-one"}

    s3 = Mock()
    s3.get_object.side_effect = get_object
    s3.put_object.side_effect = put_object

    configuration = {
        "assessor_revision": "test-assessor-v1",
        "candidate": {
            "bucket_name": "test-contract-candidates",
            "prefix": "candidates/",
            "bucket_owner_account_id": OWNER,
        },
        "baseline": {
            "bucket_name": "test-approved-contracts",
            "prefix": "sandbox-postgres/",
            "bucket_owner_account_id": OWNER,
        },
        "evidence": {
            "bucket_name": "test-contract-evidence",
            "prefix": "validation/",
            "bucket_owner_account_id": OWNER,
            "kms_key_arn": KMS_KEY,
        },
        "read_kms_key_arns": [KMS_KEY],
    }

    candidate = contract("v2")
    reference = store(
        "test-contract-candidates",
        "candidates/request-one/contract.avsc",
        candidate,
    )

    request = {
        "assessment_request_version": 1,
        "request_id": "assessment-request-one",
        "identity": {
            field: candidate[field]
            for field in (
                "source",
                "database_name",
                "schema_name",
                "table_name",
                "contract_version",
            )
        },
        "candidate_reference": reference,
        "baseline_reference": None,
        "assessor_revision": "test-assessor-v1",
    }

    service = ContractAssessmentService(
        s3_client=s3,
        configuration=configuration,
        validate_contract=validate_contract,
        canonicalise_schema=main.canonicalise_schema,
    )

    return {
        "service": service,
        "s3": s3,
        "objects": objects,
        "writes": writes,
        "store": store,
        "request": request,
    }


def assess(setup):
    return setup["service"].assess(request=setup["request"])


def add_baseline(setup):
    setup["request"]["baseline_reference"] = setup["store"](
        "test-approved-contracts",
        "sandbox-postgres/dms_test/public/example/v1/contract.avsc",
        contract("v1"),
    )


def test_first_contract_produces_exact_versioned_evidence(
    assessment_setup,
):
    setup = assessment_setup

    result = assess(setup)

    assert result["status"] == "ASSESSMENT_COMPLETED"
    assert result["validation"] == "VALID"
    assert result["compatibility"] == "FIRST_CONTRACT"

    reference = result["evidence_reference"]
    stored = setup["objects"][
        (reference["bucket_name"], reference["object_key"])
    ]
    document = json.loads(stored["content"])

    assert reference["version_id"] == "evidence-version-one"
    assert reference["sha256"] == hashlib.sha256(
        stored["content"]
    ).hexdigest()

    assert document == {
        "validation_result_version": 1,
        **{
            key: value
            for key, value in setup["request"].items()
            if key != "assessment_request_version"
        },
        "validation": "VALID",
        "compatibility": "FIRST_CONTRACT",
    }


def test_version_only_change_reports_no_change(assessment_setup):
    setup = assessment_setup
    add_baseline(setup)

    assert assess(setup)["compatibility"] == "NO_CHANGE"


def test_nullable_addition_reports_compatible(assessment_setup):
    setup = assessment_setup
    add_baseline(setup)

    candidate = contract("v2")
    candidate["fields"].append(
        {
            "name": "description",
            "type": ["null", "string"],
            "default": None,
        }
    )
    setup["request"]["candidate_reference"] = setup["store"](
        "test-contract-candidates",
        "candidates/request-one/contract.avsc",
        candidate,
    )

    assert assess(setup)["compatibility"] == "COMPATIBLE"


def test_required_addition_reports_breaking(assessment_setup):
    setup = assessment_setup
    add_baseline(setup)

    candidate = contract("v2")
    candidate["fields"].append(
        {"name": "description", "type": "string"}
    )
    setup["request"]["candidate_reference"] = setup["store"](
        "test-contract-candidates",
        "candidates/request-one/contract.avsc",
        candidate,
    )

    assert assess(setup)["compatibility"] == "BREAKING"


def test_duplicate_assessment_returns_original_evidence(
    assessment_setup,
):
    setup = assessment_setup

    first = assess(setup)
    second = assess(setup)

    assert second == first
    assert len(setup["writes"]) == 1


def test_different_existing_evidence_blocks(assessment_setup):
    setup = assessment_setup
    result = assess(setup)
    reference = result["evidence_reference"]

    setup["objects"][
        (reference["bucket_name"], reference["object_key"])
    ]["content"] = b'{"different":true}'

    with pytest.raises(
        ContractAssessmentError,
        match="different evidence",
    ):
        assess(setup)


def test_wrong_checksum_prevents_evidence_write(assessment_setup):
    setup = assessment_setup
    setup["request"]["candidate_reference"]["sha256"] = "0" * 64

    with pytest.raises(ContractAssessmentError, match="checksum"):
        assess(setup)

    setup["s3"].put_object.assert_not_called()


def test_untrusted_candidate_location_prevents_read(assessment_setup):
    setup = assessment_setup
    setup["request"]["candidate_reference"]["bucket_name"] = (
        "another-bucket"
    )

    with pytest.raises(
        ContractAssessmentError,
        match="trusted location",
    ):
        assess(setup)

    setup["s3"].get_object.assert_not_called()
    setup["s3"].put_object.assert_not_called()


def test_wrong_table_identity_prevents_evidence_write(
    assessment_setup,
):
    setup = assessment_setup
    setup["request"]["identity"]["table_name"] = "another_table"

    with pytest.raises(ContractAssessmentError, match="identity"):
        assess(setup)

    setup["s3"].put_object.assert_not_called()


@pytest.mark.parametrize("version", [True, "1", 1.0, 2])
def test_invalid_request_version_prevents_reads(
    assessment_setup,
    version,
):
    setup = assessment_setup
    setup["request"]["assessment_request_version"] = version

    with pytest.raises(ContractAssessmentError, match="revision"):
        assess(setup)

    setup["s3"].get_object.assert_not_called()


def test_access_failure_propagates_without_evidence_write(
    assessment_setup,
):
    setup = assessment_setup
    setup["s3"].get_object.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied"}},
        "GetObject",
    )

    with pytest.raises(ClientError):
        assess(setup)

    setup["s3"].put_object.assert_not_called()


def test_assessment_does_not_mutate_request(assessment_setup):
    setup = assessment_setup
    original = copy.deepcopy(setup["request"])

    assess(setup)

    assert setup["request"] == original

def configure_handler(setup, monkeypatch):
    monkeypatch.setenv(
        "CONTRACT_ASSESSMENT_CONFIG",
        json.dumps(setup["service"].configuration),
    )
    monkeypatch.setattr(main, "s3", setup["s3"])


def test_handler_assesses_candidate_without_s3_event_processing(
    assessment_setup,
    monkeypatch,
):
    setup = assessment_setup
    configure_handler(setup, monkeypatch)

    process_event = Mock()
    monkeypatch.setattr(main, "process_event", process_event)

    result = main.lambda_handler(
        {
            "operation": "ASSESS_CONTRACT",
            "request": setup["request"],
        },
        None,
    )

    assert result["status"] == "ASSESSMENT_COMPLETED"
    assert result["compatibility"] == "FIRST_CONTRACT"
    assert result["evidence_reference"]["version_id"] == (
        "evidence-version-one"
    )
    process_event.assert_not_called()


def test_handler_repeated_assessment_returns_same_evidence(
    assessment_setup,
    monkeypatch,
):
    setup = assessment_setup
    configure_handler(setup, monkeypatch)

    event = {
        "operation": "ASSESS_CONTRACT",
        "request": setup["request"],
    }

    first = main.lambda_handler(event, None)
    second = main.lambda_handler(event, None)

    assert second == first
    assert len(setup["writes"]) == 1


def test_handler_assessment_requires_deployment_configuration(
    assessment_setup,
    monkeypatch,
):
    setup = assessment_setup
    monkeypatch.delenv(
        "CONTRACT_ASSESSMENT_CONFIG",
        raising=False,
    )

    with pytest.raises(ValueError, match="not configured"):
        main.lambda_handler(
            {
                "operation": "ASSESS_CONTRACT",
                "request": setup["request"],
            },
            None,
        )

    setup["s3"].get_object.assert_not_called()
    setup["s3"].put_object.assert_not_called()


@pytest.mark.parametrize(
    "event",
    [
        {"operation": "UNKNOWN"},
        {"operation": "ASSESS_CONTRACT"},
        {
            "operation": "ASSESS_CONTRACT",
            "request": {},
            "Records": [],
        },
    ],
)
def test_handler_rejects_unsupported_assessment_envelopes(event):
    with pytest.raises(ValueError):
        main.lambda_handler(event, None)


def test_handler_rejects_malformed_configuration(
    assessment_setup,
    monkeypatch,
):
    setup = assessment_setup
    monkeypatch.setenv("CONTRACT_ASSESSMENT_CONFIG", "{")

    with pytest.raises(ValueError, match="valid JSON"):
        main.lambda_handler(
            {
                "operation": "ASSESS_CONTRACT",
                "request": setup["request"],
            },
            None,
        )

    setup["s3"].get_object.assert_not_called()


def test_handler_rejects_different_assessor_revision(
    assessment_setup,
    monkeypatch,
):
    setup = assessment_setup
    configure_handler(setup, monkeypatch)
    setup["request"]["assessor_revision"] = "another-assessor"

    with pytest.raises(ContractAssessmentError, match="revision"):
        main.lambda_handler(
            {
                "operation": "ASSESS_CONTRACT",
                "request": setup["request"],
            },
            None,
        )

    setup["s3"].get_object.assert_not_called()
    setup["s3"].put_object.assert_not_called()