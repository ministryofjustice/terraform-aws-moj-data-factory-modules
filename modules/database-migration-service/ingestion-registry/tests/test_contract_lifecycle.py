from dataclasses import replace

import boto3
import pytest
from moto import mock_aws

from ingestion_registry.admission import IngestionRegistry
from ingestion_registry.contract_lifecycle import (
    ContractLifecycle,
    ContractRequestConflict,
)
from ingestion_registry.contract_policy import (
    CompatibilityOutcome,
    ContractAssessment,
    ContractPolicy,
    ValidationOutcome,
)
from ingestion_registry.identity import TableIdentity
from ingestion_registry.references import S3ObjectReference


def reference(key, version):
    return S3ObjectReference(
        bucket_name="example-contract-evidence",
        object_key=key,
        version_id=version,
        sha256="a" * 64,
    )


@pytest.fixture
def lifecycle_setup():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="eu-west-2")
        client.create_table(
            TableName="test-contract-lifecycle",
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
            client,
            table_name="test-contract-lifecycle",
        )
        table = TableIdentity(
            environment="development",
            source="sandbox-postgres",
            database_name="dms_test",
            schema_name="public",
            table_name="dms_integration_test",
        )
        assessment = ContractAssessment(
            validation=ValidationOutcome.VALID,
            compatibility=CompatibilityOutcome.COMPATIBLE,
            candidate_reference=reference(
                "candidates/request-one/contract.avsc",
                "candidate-one",
            ),
            baseline_reference=reference(
                "contracts/baseline.avsc",
                "baseline-one",
            ),
            evidence_reference=reference(
                "validation/request-one/result.json",
                "validation-one",
            ),
        )
        yield registry, ContractLifecycle(registry), table, assessment


def submit(setup, **overrides):
    _, lifecycle, table, assessment = setup
    arguments = {
        "table": table,
        "request_id": "request-one",
        "assessment": assessment,
        "policy": ContractPolicy(revision="policy-v1"),
    }
    arguments.update(overrides)
    return lifecycle.submit_assessment(**arguments)


def test_records_pending_review_without_an_ingestion(lifecycle_setup):
    registry, lifecycle, table, _ = lifecycle_setup

    result = submit(lifecycle_setup)

    assert result["status"] == "PENDING_REVIEW"
    assert result["revision"] == 1
    assert result["assessment"]["candidate_reference"]["version_id"] == (
        "candidate-one"
    )
    assert result["policy"]["revision"] == "policy-v1"
    assert lifecycle.read(
        table=table, request_id="request-one"
    ) == result

    items = registry.client.scan(
        TableName=registry.table_name,
        ConsistentRead=True,
    )["Items"]
    assert len(items) == 1
    assert "ingestion_id" not in result
    assert "ingestion_version" not in result


def test_duplicate_returns_original_record(lifecycle_setup):
    first = submit(lifecycle_setup)
    second = submit(lifecycle_setup)

    assert second == first


@pytest.mark.parametrize(
    "field,replacement",
    [
        (
            "candidate_reference",
            reference("candidates/request-one/contract.avsc", "candidate-two"),
        ),
        (
            "baseline_reference",
            reference("contracts/baseline.avsc", "baseline-two"),
        ),
        (
            "evidence_reference",
            reference("validation/request-one/result.json", "validation-two"),
        ),
    ],
)
def test_request_cannot_be_reused_for_changed_evidence(
    lifecycle_setup, field, replacement
):
    submit(lifecycle_setup)
    changed = replace(
        lifecycle_setup[3],
        **{field: replacement},
    )

    with pytest.raises(ContractRequestConflict):
        submit(lifecycle_setup, assessment=changed)


def test_request_cannot_silently_change_policy(lifecycle_setup):
    submit(lifecycle_setup)

    with pytest.raises(ContractRequestConflict):
        submit(
            lifecycle_setup,
            policy=ContractPolicy(
                revision="policy-v2",
                automatically_accept_compatible=True,
            ),
        )


@pytest.mark.parametrize(
    "compatibility,status",
    [
        (CompatibilityOutcome.NO_CHANGE, "REUSE_PENDING"),
        (CompatibilityOutcome.BREAKING, "PENDING_REVIEW"),
        (CompatibilityOutcome.UNSUPPORTED, "BLOCKED"),
    ],
)
def test_records_required_progression_state(
    lifecycle_setup, compatibility, status
):
    result = submit(
        lifecycle_setup,
        assessment=replace(
            lifecycle_setup[3],
            compatibility=compatibility,
        ),
    )

    assert result["status"] == status


def test_explicit_policy_acceptance_is_not_activation(lifecycle_setup):
    result = submit(
        lifecycle_setup,
        policy=ContractPolicy(
            revision="policy-v2",
            automatically_accept_compatible=True,
        ),
    )

    assert result["status"] == "POLICY_ACCEPTED"
    assert "approval_reference" not in result
    assert "registered_contract" not in result


def test_tables_have_independent_request_namespaces(lifecycle_setup):
    first = submit(lifecycle_setup)
    second = submit(
        lifecycle_setup,
        table=replace(
            lifecycle_setup[2],
            table_name="another_table",
        ),
    )

    assert first["pk"] != second["pk"]


@pytest.mark.parametrize("request_id", ["", " request", None])
def test_invalid_request_id_is_rejected(lifecycle_setup, request_id):
    with pytest.raises(ValueError, match="request_id"):
        submit(lifecycle_setup, request_id=request_id)