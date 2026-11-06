import importlib.util
import io
from pathlib import Path
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError


spec = importlib.util.spec_from_file_location(
    "generation_candidate_storage_under_test",
    Path(__file__).with_name("candidate_storage.py"),
)
storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storage)


KMS_KEY = (
    "arn:aws:kms:eu-west-2:123456789012:"
    "key/11111111-2222-3333-4444-555555555555"
)


@pytest.fixture
def candidate_storage_setup():
    content = b'{"example":"validated contract bytes"}'
    body = io.BytesIO(content)
    client = Mock()
    client.put_object.return_value = {
        "VersionId": "candidate-one"
    }
    client.get_object.return_value = {
        "Body": body,
        "VersionId": "candidate-one",
        "ServerSideEncryption": "aws:kms",
        "SSEKMSKeyId": KMS_KEY,
    }

    arguments = {
        "s3_client": client,
        "content": content,
        "identity": {
            "source": "sandbox-postgres",
            "database_name": "dms_test",
            "schema_name": "public",
            "table_name": "dms_integration_test",
            "contract_version": "v1",
        },
        "request_id": "request-one",
        "bucket_name": "example-candidates",
        "bucket_owner_account_id": "123456789012",
        "kms_key_arn": KMS_KEY,
    }
    return arguments, client, body


def precondition_failure():
    return ClientError(
        {
            "Error": {"Code": "PreconditionFailed"},
            "ResponseMetadata": {"HTTPStatusCode": 412},
        },
        "PutObject",
    )


def test_stores_versioned_candidate_separately(
    candidate_storage_setup,
):
    arguments, client, body = candidate_storage_setup

    result = storage.store_candidate(**arguments)

    request = client.put_object.call_args.kwargs
    assert request["Key"].startswith("candidates/")
    assert request["Key"].endswith("/contract.avsc")
    assert request["Body"] == arguments["content"]
    assert request["IfNoneMatch"] == "*"
    assert request["ServerSideEncryption"] == "aws:kms"
    assert request["SSEKMSKeyId"] == KMS_KEY
    assert request["ExpectedBucketOwner"] == "123456789012"

    assert result["status"] == "CANDIDATE_STORED"
    assert result["candidate_reference"]["version_id"] == "candidate-one"
    assert result["candidate_reference"]["object_key"] == request["Key"]
    assert body.closed


def test_identical_existing_candidate_is_reconciled(
    candidate_storage_setup,
):
    arguments, client, body = candidate_storage_setup
    client.put_object.side_effect = precondition_failure()

    result = storage.store_candidate(**arguments)

    assert result["candidate_reference"]["version_id"] == "candidate-one"
    assert "VersionId" not in client.get_object.call_args.kwargs
    assert client.put_object.call_count == 1
    assert body.closed


def test_changed_bytes_for_same_request_are_rejected(
    candidate_storage_setup,
):
    arguments, client, original_body = candidate_storage_setup
    client.put_object.side_effect = precondition_failure()
    replacement = io.BytesIO(b"different bytes")
    client.get_object.return_value["Body"] = replacement

    with pytest.raises(
        storage.CandidateStorageConflict,
        match="different bytes",
    ):
        storage.store_candidate(**arguments)

    assert replacement.closed
    assert client.put_object.call_count == 1
    original_body.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("VersionId", "null"),
        ("VersionId", "different-version"),
        ("ServerSideEncryption", "AES256"),
        ("SSEKMSKeyId", "another-key"),
    ],
)
def test_invalid_stored_evidence_is_rejected(
    candidate_storage_setup, field, value
):
    arguments, client, body = candidate_storage_setup
    client.get_object.return_value[field] = value

    with pytest.raises(storage.CandidateStorageConflict):
        storage.store_candidate(**arguments)

    assert body.closed


@pytest.mark.parametrize("request_id", ["", " request", None])
def test_invalid_request_is_rejected_before_write(
    candidate_storage_setup, request_id
):
    arguments, client, body = candidate_storage_setup
    arguments["request_id"] = request_id

    with pytest.raises(ValueError, match="request_id"):
        storage.store_candidate(**arguments)

    client.put_object.assert_not_called()
    body.close()


def test_access_failure_is_not_treated_as_existing_candidate(
    candidate_storage_setup,
):
    arguments, client, body = candidate_storage_setup
    error = ClientError(
        {"Error": {"Code": "AccessDenied"}},
        "PutObject",
    )
    client.put_object.side_effect = error

    with pytest.raises(ClientError) as raised:
        storage.store_candidate(**arguments)

    assert raised.value is error
    client.get_object.assert_not_called()
    body.close()