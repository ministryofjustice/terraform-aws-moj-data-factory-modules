"""Shared Moto fixtures and helpers for contract integration tests."""

import copy
import hashlib
import importlib.util
import io
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ReadTimeoutError
from moto import mock_aws

from ingestion_registry.admission import IngestionRegistry
from ingestion_registry.contract_approval import ContractApprovalGate
from ingestion_registry.contract_candidate import ContractCandidateReader
from ingestion_registry.contract_lifecycle import ContractLifecycle
from ingestion_registry.contract_policy import (
    CompatibilityOutcome,
    ContractAssessment,
    ContractPolicy,
    ValidationOutcome,
)
from ingestion_registry.contract_promotion import ContractPromoter
from ingestion_registry.contract_registration import ContractRegistrar
from ingestion_registry.contract_registration_completion import (
    ContractRegistrationCompletion,
)
from ingestion_registry.contract_verification import RegisteredContractVerifier
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
