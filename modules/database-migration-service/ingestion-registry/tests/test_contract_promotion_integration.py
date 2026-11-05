import copy
import hashlib
import json
import io

import boto3
import pytest
import importlib.util
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ingestion_registry.contract_registration import ContractRegistrar
from ingestion_registry.contract_registration_completion import (
    ContractRegistrationCompletion,
)
from ingestion_registry.contract_verification import RegisteredContractVerifier
from botocore.exceptions import ReadTimeoutError
from moto import mock_aws

from ingestion_registry.admission import IngestionRegistry
from ingestion_registry.contract_approval import (
    ContractApprovalBlocked,
    ContractApprovalGate,
)
from ingestion_registry.contract_candidate import ContractCandidateReader
from ingestion_registry.contract_lifecycle import ContractLifecycle
from ingestion_registry.contract_policy import (
    CompatibilityOutcome,
    ContractAssessment,
    ContractPolicy,
    ValidationOutcome,
)
from ingestion_registry.contract_promotion import (
    ContractPromoter,
    ContractPromotionConflict,
)
from ingestion_registry.identity import TableIdentity
from ingestion_registry.references import S3ObjectReference


ACCOUNT_ID = "123456789012"
REGION = "eu-west-2"


def encode(document):
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def store_reference(s3, bucket, key, content):
    response = s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=content,
    )
    return S3ObjectReference(
        bucket_name=bucket,
        object_key=key,
        version_id=response["VersionId"],
        sha256=hashlib.sha256(content).hexdigest(),
    )


@pytest.fixture
def promotion_integration():
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        dynamodb = boto3.client("dynamodb", region_name=REGION)
        kms = boto3.client("kms", region_name=REGION)

        for bucket in (
            "test-contract-candidates",
            "test-contract-evidence",
            "test-approved-contracts",
        ):
            s3.create_bucket(
                Bucket=bucket,
                CreateBucketConfiguration={
                    "LocationConstraint": REGION
                },
            )
            s3.put_bucket_versioning(
                Bucket=bucket,
                VersioningConfiguration={"Status": "Enabled"},
            )

        kms_key = kms.create_key()["KeyMetadata"]["Arn"]

        dynamodb.create_table(
            TableName="test-contract-control",
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        registry = IngestionRegistry(
            dynamodb,
            table_name="test-contract-control",
        )
        lifecycle = ContractLifecycle(registry)

        table = TableIdentity(
            environment="development",
            source="sandbox-postgres",
            database_name="dms_test",
            schema_name="public",
            table_name="dms_integration_test",
        )

        contract = {
            "source": table.source,
            "database_name": table.database_name,
            "schema_name": table.schema_name,
            "table_name": table.table_name,
            "contract_version": "v1",
            "type": "record",
            "name": table.table_name,
            "fields": [{"name": "id", "type": "long"}],
        }
        raw = encode(contract)

        candidate = store_reference(
            s3,
            "test-contract-candidates",
            "candidates/request-one/contract.avsc",
            raw,
        )

        validation = store_reference(
            s3,
            "test-contract-evidence",
            "validation/request-one/result.json",
            encode(
                {
                    "validation": "VALID",
                    "compatibility": "FIRST_CONTRACT",
                }
            ),
        )

        assessment = ContractAssessment(
            validation=ValidationOutcome.VALID,
            compatibility=CompatibilityOutcome.FIRST_CONTRACT,
            candidate_reference=candidate,
            baseline_reference=None,
            evidence_reference=validation,
        )

        request = lifecycle.submit_assessment(
            table=table,
            request_id="request-one",
            assessment=assessment,
            policy=ContractPolicy(revision="policy-v1"),
        )

        def authorise(reviewer, selected_table):
            if (
                reviewer != "test-authorised-reviewer"
                or selected_table != table
            ):
                raise PermissionError("Reviewer is not permitted")

        gate = ContractApprovalGate(
            lifecycle=lifecycle,
            s3_client=s3,
            approval_bucket="test-contract-evidence",
            approval_prefix="approvals/",
            bucket_owner_account_id=ACCOUNT_ID,
            authorise=authorise,
        )

        reader = ContractCandidateReader(
            s3_client=s3,
            candidate_bucket="test-contract-candidates",
            candidate_prefix="candidates/",
            bucket_owner_account_id=ACCOUNT_ID,
        )

        promoter = ContractPromoter(
            approval_gate=gate,
            candidate_reader=reader,
            s3_client=s3,
            destination_bucket="test-approved-contracts",
            bucket_owner_account_id=ACCOUNT_ID,
            kms_key_arn=kms_key,
        )

        yield {
            "s3": s3,
            "registry": registry,
            "lifecycle": lifecycle,
            "table": table,
            "request": request,
            "candidate": candidate,
            "contract": contract,
            "raw": raw,
            "kms_key": kms_key,
            "authorise": authorise,
            "promoter": promoter,
        }


def approve(setup):
    request = setup["request"]

    document = {
        "approval_version": 1,
        "table_identity": request["table_identity"],
        "request_id": request["request_id"],
        "request_fingerprint": request["request_fingerprint"],
        "review_id": "review-one",
        "reviewer": "test-authorised-reviewer",
        "outcome": "APPROVED",
        "assessment": copy.deepcopy(request["assessment"]),
        "policy": copy.deepcopy(request["policy"]),
    }

    reference = store_reference(
        setup["s3"],
        "test-contract-evidence",
        "approvals/request-one/review-one.json",
        encode(document),
    )

    return setup["lifecycle"].record_review(
        table=setup["table"],
        request_id="request-one",
        review_id="review-one",
        reviewer="test-authorised-reviewer",
        outcome="APPROVED",
        expected_revision=1,
        expected_request_fingerprint=request["request_fingerprint"],
        evidence_reference=reference,
        authorise=setup["authorise"],
    )


def promote(setup):
    return setup["promoter"].promote(
        table=setup["table"],
        request_id="request-one",
        contract_version="v1",
    )


def read_promoted(setup, result):
    reference = result["object_reference"]
    response = setup["s3"].get_object(
        Bucket=reference["bucket_name"],
        Key=reference["object_key"],
        VersionId=reference["version_id"],
    )
    body = response["Body"]
    try:
        return response, body.read()
    finally:
        body.close()


def test_real_approval_and_candidate_are_promoted(
    promotion_integration,
):
    setup = promotion_integration
    approve(setup)

    result = promote(setup)
    response, content = read_promoted(setup, result)

    assert content == setup["raw"]
    assert result["candidate_reference"]["version_id"] == (
        setup["candidate"].version_id
    )
    assert result["contract_fingerprint"] == hashlib.sha256(
        setup["raw"]
    ).hexdigest()
    assert response["ServerSideEncryption"] == "aws:kms"
    assert response["SSEKMSKeyId"] == setup["kms_key"]

    current = setup["lifecycle"].read(
        table=setup["table"],
        request_id="request-one",
    )
    assert current["status"] == "APPROVED"
    assert current["revision"] == 2
    assert "registered_contract" not in current

    items = setup["registry"].client.scan(
        TableName=setup["registry"].table_name,
        ConsistentRead=True,
    )["Items"]
    records = [
        {
            key: setup["registry"].deserializer.deserialize(value)
            for key, value in item.items()
        }
        for item in items
    ]
    assert {item["record_type"] for item in records} == {
        "CONTRACT_REQUEST",
        "CONTRACT_REVIEW",
        "CONTRACT_REGISTRATION_AUTHORIZATION",
    }


def test_pending_review_creates_no_destination_object(
    promotion_integration,
):
    setup = promotion_integration

    with pytest.raises(
        ContractApprovalBlocked, match="not currently approved"
    ):
        promote(setup)

    assert setup["s3"].list_objects_v2(
        Bucket="test-approved-contracts"
    ).get("KeyCount") == 0

    assert setup["registry"]._read(
        setup["request"]["pk"],
        "REGISTRATION_AUTHORIZATION",
    ) is None


def test_retry_preserves_the_original_promoted_version(
    promotion_integration,
):
    setup = promotion_integration
    approve(setup)

    first = promote(setup)
    second = promote(setup)

    assert second == first

    versions = setup["s3"].list_object_versions(
        Bucket="test-approved-contracts"
    )["Versions"]
    assert len(versions) == 1


def test_new_candidate_version_does_not_change_approved_bytes(
    promotion_integration,
):
    setup = promotion_integration
    approve(setup)

    changed = copy.deepcopy(setup["contract"])
    changed["fields"].append(
        {"name": "extra", "type": ["null", "string"], "default": None}
    )
    newer = setup["s3"].put_object(
        Bucket=setup["candidate"].bucket_name,
        Key=setup["candidate"].object_key,
        Body=encode(changed),
    )

    assert newer["VersionId"] != setup["candidate"].version_id

    result = promote(setup)
    _, content = read_promoted(setup, result)

    assert content == setup["raw"]


def test_existing_conflicting_contract_is_not_overwritten(
    promotion_integration,
):
    setup = promotion_integration
    approve(setup)
    key = (
        "sandbox-postgres/dms_test/public/"
        "dms_integration_test/v1/contract.avsc"
    )

    original = setup["s3"].put_object(
        Bucket="test-approved-contracts",
        Key=key,
        Body=b"different contract bytes",
        ServerSideEncryption="aws:kms",
        SSEKMSKeyId=setup["kms_key"],
    )

    with pytest.raises(ContractPromotionConflict, match="different bytes"):
        promote(setup)

    response = setup["s3"].get_object(
        Bucket="test-approved-contracts",
        Key=key,
    )
    body = response["Body"]
    try:
        assert body.read() == b"different contract bytes"
    finally:
        body.close()

    assert response["VersionId"] == original["VersionId"]
    assert len(
        setup["s3"].list_object_versions(
            Bucket="test-approved-contracts"
        )["Versions"]
    ) == 1


def test_lost_success_response_is_reconciled_on_retry(
    promotion_integration, monkeypatch
):
    setup = promotion_integration
    approve(setup)
    original_put = setup["s3"].put_object

    def write_then_lose_response(**kwargs):
        original_put(**kwargs)
        raise ReadTimeoutError(
            endpoint_url="https://s3.eu-west-2.amazonaws.com"
        )

    monkeypatch.setattr(
        setup["s3"], "put_object", write_then_lose_response
    )

    with pytest.raises(ReadTimeoutError):
        promote(setup)

    monkeypatch.setattr(setup["s3"], "put_object", original_put)

    result = promote(setup)
    _, content = read_promoted(setup, result)

    assert content == setup["raw"]
    assert len(
        setup["s3"].list_object_versions(
            Bucket="test-approved-contracts"
        )["Versions"]
    ) == 1

class LocalRegistrationInvocation:
    """Execute the real handler through a simulated Lambda transport."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = 0
        self.lose_next_response = False

    def invoke(self, *, FunctionName, InvocationType, Payload):
        assert FunctionName == (
            "arn:aws:lambda:eu-west-2:123456789012:"
            "function:contract-registration:7"
        )
        assert InvocationType == "RequestResponse"

        self.calls += 1
        result = self.handler(
            json.loads(Payload),
            SimpleNamespace(
                aws_request_id=f"registration-call-{self.calls}"
            ),
        )

        if self.lose_next_response:
            self.lose_next_response = False
            raise ReadTimeoutError(
                endpoint_url="https://lambda.eu-west-2.amazonaws.com"
            )

        return {
            "StatusCode": 200,
            "ExecutedVersion": "7",
            "Payload": io.BytesIO(encode(result)),
        }


@pytest.fixture
def registration_integration(promotion_integration, monkeypatch):
    setup = promotion_integration

    client = setup["registry"].client
    client.create_table(
        TableName="test-contract-registration",
        KeySchema=[
            {"AttributeName": "contract_id", "KeyType": "HASH"},
            {"AttributeName": "contract_version", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "contract_id", "AttributeType": "S"},
            {"AttributeName": "contract_version", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    monkeypatch.setenv("AUDIT_TABLE_NAME", "test-contract-registration")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)

    runtime = (
        Path(__file__).resolve().parents[2]
        / "modules"
        / "data-contract-management"
        / "runtime"
        / "schema-registration"
    )

    # Load the component's logger without replacing any other runtime's
    # logger module permanently.
    logger_spec = importlib.util.spec_from_file_location(
        "contract_registration_integration_logger",
        runtime / "logger.py",
    )
    logger_module = importlib.util.module_from_spec(logger_spec)
    logger_spec.loader.exec_module(logger_module)

    handler_spec = importlib.util.spec_from_file_location(
        "contract_registration_integration_handler",
        runtime / "main.py",
    )
    handler_module = importlib.util.module_from_spec(handler_spec)

    with patch.dict(sys.modules, {"logger": logger_module}):
        handler_spec.loader.exec_module(handler_module)

    registration_table = boto3.resource(
        "dynamodb",
        region_name=REGION,
    ).Table("test-contract-registration")

    verifier = RegisteredContractVerifier(
        contract_table=registration_table,
        s3_client=setup["s3"],
        bucket_name="test-approved-contracts",
        bucket_owner_account_id=ACCOUNT_ID,
    )

    invocation = LocalRegistrationInvocation(
        handler_module.lambda_handler
    )
    gate = setup["promoter"].approval_gate

    registrar = ContractRegistrar(
        promoter=setup["promoter"],
        approval_gate=gate,
        verifier=verifier,
        lambda_client=invocation,
        registration_function_arn=(
            "arn:aws:lambda:eu-west-2:123456789012:"
            "function:contract-registration:7"
        ),
    )

    completion = ContractRegistrationCompletion(
        registrar=registrar,
        approval_gate=gate,
    )

    yield {
        **setup,
        "registration_table": registration_table,
        "invocation": invocation,
        "completion": completion,
    }


def complete_registration_integration(setup):
    return setup["completion"].complete(
        table=setup["table"],
        request_id="request-one",
        contract_version="v1",
    )


def expected_contract_id(table):
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_DNS,
            ":".join(
                (
                    table.source,
                    table.database_name,
                    table.schema_name,
                    table.table_name,
                )
            ),
        )
    )


def test_actual_handler_registers_promoted_approved_contract(
    registration_integration,
):
    setup = registration_integration
    approve(setup)

    completion = complete_registration_integration(setup)
    registered = completion["result"]["registered_contract"]

    assert setup["invocation"].calls == 1
    assert registered["contract_id"] == expected_contract_id(
        setup["table"]
    )
    assert registered["contract_version"] == "v1"
    assert registered["contract_fingerprint"] == hashlib.sha256(
        setup["raw"]
    ).hexdigest()

    item = setup["registration_table"].get_item(
        Key={
            "contract_id": registered["contract_id"],
            "contract_version": "v1",
        },
        ConsistentRead=True,
    )["Item"]

    assert item["schema_fingerprint"] == (
        registered["contract_fingerprint"]
    )
    assert item["s3_object_version_id"] == (
        registered["object_reference"]["version_id"]
    )
    assert item["is_active"] is False
    assert item["ingestion_version"] is None

    assert setup["registry"]._read(
        setup["request"]["pk"],
        "REGISTRATION",
    ) == completion


def test_completed_registration_retry_does_not_invoke_again(
    registration_integration,
):
    setup = registration_integration
    approve(setup)

    first = complete_registration_integration(setup)
    second = complete_registration_integration(setup)

    assert second == first
    assert setup["invocation"].calls == 1
    assert len(
        setup["s3"].list_object_versions(
            Bucket="test-approved-contracts"
        )["Versions"]
    ) == 1


def test_lost_registration_response_is_reconciled(
    registration_integration,
):
    setup = registration_integration
    approve(setup)
    setup["invocation"].lose_next_response = True

    with pytest.raises(ReadTimeoutError):
        complete_registration_integration(setup)

    # The handler committed registration, but the orchestration did
    # not receive confirmation and must not yet record completion.
    assert setup["registry"]._read(
        setup["request"]["pk"],
        "REGISTRATION",
    ) is None

    completion = complete_registration_integration(setup)

    assert completion["record_type"] == "CONTRACT_REGISTRATION"
    assert setup["invocation"].calls == 1


def test_pending_review_never_invokes_registration(
    registration_integration,
):
    setup = registration_integration

    with pytest.raises(
        ContractApprovalBlocked,
        match="not currently approved",
    ):
        complete_registration_integration(setup)

    assert setup["invocation"].calls == 0
    assert setup["registration_table"].scan(
        ConsistentRead=True
    )["Count"] == 0


def test_conflicting_registration_blocks_completion(
    registration_integration,
):
    from ingestion_registry.contract_verification import (
        ContractVerificationError,
    )

    setup = registration_integration
    approve(setup)

    # Create the approved final object, but do not register it yet.
    promoted = promote(setup)

    # Point the registration at a real object version while recording
    # a conflicting fingerprint. Verification must reject the mismatch.
    setup["registration_table"].put_item(
        Item={
            "contract_id": expected_contract_id(setup["table"]),
            "contract_version": "v1",
            "source": setup["table"].source,
            "database_name": setup["table"].database_name,
            "schema_name": setup["table"].schema_name,
            "table_name": setup["table"].table_name,
            "schema_fingerprint": "f" * 64,
            "s3_contract_uri": promoted["s3_contract_uri"],
            "s3_object_version_id": promoted[
                "object_reference"
            ]["version_id"],
            "is_active": False,
        }
    )

    with pytest.raises(
        ContractVerificationError,
        match="fingerprint",
    ):
        complete_registration_integration(setup)

    assert setup["invocation"].calls == 0
    assert setup["registry"]._read(
        setup["request"]["pk"],
        "REGISTRATION",
    ) is None