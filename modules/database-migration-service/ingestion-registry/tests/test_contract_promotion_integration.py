"""Integration tests for approved candidate promotion."""

import copy
import hashlib

import pytest
from botocore.exceptions import ReadTimeoutError

from contract_integration_support import (
    approve,
    encode,
    promote,
    read_promoted,
)
from ingestion_registry.contract_approval import ContractApprovalBlocked
from ingestion_registry.contract_promotion import ContractPromotionConflict


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