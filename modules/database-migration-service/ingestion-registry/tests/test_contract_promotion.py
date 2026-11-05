import copy
import hashlib
import io
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from ingestion_registry.contract_candidate import VerifiedCandidate
from ingestion_registry.contract_promotion import (
    ContractPromoter,
    ContractPromotionConflict,
)
from ingestion_registry.identity import TableIdentity
from ingestion_registry.references import S3ObjectReference


KMS_KEY = (
    "arn:aws:kms:eu-west-2:123456789012:"
    "key/11111111-2222-3333-4444-555555555555"
)


def aws_error(code, status):
    return ClientError(
        {
            "Error": {"Code": code, "Message": "Test error"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "PutObject",
    )


@pytest.fixture
def promotion_setup():
    table = TableIdentity(
        environment="development",
        source="sandbox-postgres",
        database_name="dms_test",
        schema_name="public",
        table_name="dms_integration_test",
    )
    content = b'{"candidate":"exact approved bytes"}'
    reference = S3ObjectReference(
        bucket_name="example-candidates",
        object_key="candidates/request-one/contract.avsc",
        version_id="candidate-one",
        sha256=hashlib.sha256(content).hexdigest(),
    )

    authorisation = {
        "pk": "CONTRACTREQUEST#example",
        "request_fingerprint": "b" * 64,
        "approval_revision": 2,
        "approval_reference": {
            "bucket_name": "example-approvals",
            "object_key": "approvals/request-one.json",
            "version_id": "approval-one",
            "sha256": "c" * 64,
        },
        "assessment": {
            "candidate_reference": {
                "bucket_name": reference.bucket_name,
                "object_key": reference.object_key,
                "version_id": reference.version_id,
                "sha256": reference.sha256,
            }
        },
    }

    gate = Mock()
    gate.authorise_registration.return_value = authorisation

    reader = Mock()
    reader.read.return_value = VerifiedCandidate(
        reference=reference,
        contract_version="v1",
        contract_fingerprint="d" * 64,
        content=content,
    )

    s3 = Mock()
    s3.put_object.return_value = {"VersionId": "promoted-one"}
    body = io.BytesIO(content)
    s3.get_object.return_value = {
        "Body": body,
        "VersionId": "promoted-one",
        "ServerSideEncryption": "aws:kms",
        "SSEKMSKeyId": KMS_KEY,
    }

    promoter = ContractPromoter(
        approval_gate=gate,
        candidate_reader=reader,
        s3_client=s3,
        destination_bucket="example-contracts",
        bucket_owner_account_id="123456789012",
        kms_key_arn=KMS_KEY,
    )

    return table, gate, reader, s3, body, content, promoter


def promote(setup):
    return setup[-1].promote(
        table=setup[0],
        request_id="request-one",
        contract_version="v1",
    )


def test_promotes_exact_bytes_conditionally(promotion_setup):
    table, gate, reader, s3, body, content, _ = promotion_setup

    result = promote(promotion_setup)

    request = s3.put_object.call_args.kwargs
    assert request["Body"] == content
    assert request["IfNoneMatch"] == "*"
    assert request["ExpectedBucketOwner"] == "123456789012"
    assert request["ServerSideEncryption"] == "aws:kms"
    assert request["SSEKMSKeyId"] == KMS_KEY
    assert request["Key"] == (
        "sandbox-postgres/dms_test/public/"
        "dms_integration_test/v1/contract.avsc"
    )
    assert gate.authorise_registration.call_count == 2
    reader.read.assert_called_once_with(
        table=table,
        contract_version="v1",
        reference=reader.read.return_value.reference,
    )
    assert result["status"] == "PROMOTED"
    assert result["object_reference"]["version_id"] == "promoted-one"
    assert result["object_reference"]["sha256"] == (
        hashlib.sha256(content).hexdigest()
    )
    assert body.closed


def test_existing_identical_object_is_reconciled(promotion_setup):
    s3 = promotion_setup[3]
    s3.put_object.side_effect = aws_error("PreconditionFailed", 412)

    result = promote(promotion_setup)

    assert result["object_reference"]["version_id"] == "promoted-one"
    assert "VersionId" not in s3.get_object.call_args.kwargs
    assert s3.put_object.call_count == 1


def test_same_version_different_bytes_is_blocked(promotion_setup):
    s3 = promotion_setup[3]
    s3.put_object.side_effect = aws_error("PreconditionFailed", 412)
    replacement = io.BytesIO(b"different bytes")
    s3.get_object.return_value["Body"] = replacement

    with pytest.raises(ContractPromotionConflict, match="different bytes"):
        promote(promotion_setup)

    assert s3.put_object.call_count == 1
    assert replacement.closed
    promotion_setup[4].close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("VersionId", "null"),
        ("VersionId", "different-version"),
        ("ServerSideEncryption", "AES256"),
        ("SSEKMSKeyId", "another-key"),
    ],
)
def test_invalid_destination_evidence_is_rejected(
    promotion_setup, field, value
):
    promotion_setup[3].get_object.return_value[field] = value

    with pytest.raises(ContractPromotionConflict):
        promote(promotion_setup)

    assert promotion_setup[4].closed


def test_changed_approval_blocks_write(promotion_setup):
    gate = promotion_setup[1]
    original = gate.authorise_registration.return_value
    changed = copy.deepcopy(original)
    changed["approval_revision"] = 3
    gate.authorise_registration.side_effect = [original, changed]

    with pytest.raises(ContractPromotionConflict, match="Approval changed"):
        promote(promotion_setup)

    promotion_setup[3].put_object.assert_not_called()
    promotion_setup[4].close()


def test_candidate_failure_blocks_write(promotion_setup):
    promotion_setup[2].read.side_effect = ValueError("Invalid candidate")

    with pytest.raises(ValueError, match="Invalid candidate"):
        promote(promotion_setup)

    promotion_setup[3].put_object.assert_not_called()
    promotion_setup[4].close()


@pytest.mark.parametrize(
    "code,status",
    [
        ("AccessDenied", 403),
        ("ConditionalRequestConflict", 409),
    ],
)
def test_other_aws_errors_propagate(promotion_setup, code, status):
    error = aws_error(code, status)
    promotion_setup[3].put_object.side_effect = error

    with pytest.raises(ClientError) as raised:
        promote(promotion_setup)

    assert raised.value is error
    promotion_setup[3].get_object.assert_not_called()
    promotion_setup[4].close()