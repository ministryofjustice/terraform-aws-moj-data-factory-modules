import hashlib
import io
import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from ingestion_registry.contract_candidate import (
    ContractCandidateError,
    ContractCandidateReader,
)
from ingestion_registry.identity import TableIdentity
from ingestion_registry.references import S3ObjectReference


@pytest.fixture
def candidate_setup():
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

    raw = json.dumps(contract, indent=2).encode("utf-8")
    reference = S3ObjectReference(
        bucket_name="example-candidates",
        object_key="candidates/request-one/contract.avsc",
        version_id="candidate-one",
        sha256=hashlib.sha256(raw).hexdigest(),
    )

    body = io.BytesIO(raw)
    s3 = Mock()
    s3.get_object.return_value = {
        "Body": body,
        "VersionId": "candidate-one",
    }

    reader = ContractCandidateReader(
        s3_client=s3,
        candidate_bucket="example-candidates",
        candidate_prefix="candidates/",
        bucket_owner_account_id="123456789012",
    )
    return table, contract, raw, reference, body, s3, reader


def read_candidate(setup, **overrides):
    arguments = {
        "table": setup[0],
        "contract_version": "v1",
        "reference": setup[3],
    }
    arguments.update(overrides)
    return setup[-1].read(**arguments)


def test_reads_exact_candidate_and_preserves_bytes(candidate_setup):
    table, contract, raw, reference, body, s3, _ = candidate_setup

    result = read_candidate(candidate_setup)

    normalised = json.dumps(
        contract,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")

    assert result.content == raw
    assert result.reference == reference
    assert result.contract_version == "v1"
    assert result.contract_fingerprint == hashlib.sha256(
        normalised
    ).hexdigest()
    assert result.contract_fingerprint != reference.sha256
    assert body.closed

    s3.get_object.assert_called_once_with(
        Bucket="example-candidates",
        Key="candidates/request-one/contract.avsc",
        VersionId="candidate-one",
        ExpectedBucketOwner="123456789012",
    )
    assert [call[0] for call in s3.method_calls] == ["get_object"]


@pytest.mark.parametrize(
    "changes",
    [
        {"bucket_name": "untrusted-candidates"},
        {"object_key": "candidates-other/contract.avsc"},
    ],
)
def test_rejects_candidate_outside_configured_location(
    candidate_setup, changes
):
    reference = replace(candidate_setup[3], **changes)

    with pytest.raises(ContractCandidateError, match="location"):
        read_candidate(candidate_setup, reference=reference)

    candidate_setup[5].get_object.assert_not_called()
    candidate_setup[4].close()


def test_checksum_mismatch_is_rejected(candidate_setup):
    reference = replace(candidate_setup[3], sha256="b" * 64)

    with pytest.raises(ContractCandidateError, match="checksum"):
        read_candidate(candidate_setup, reference=reference)

    assert candidate_setup[4].closed


def test_wrong_object_version_is_rejected(candidate_setup):
    candidate_setup[5].get_object.return_value["VersionId"] = "another"

    with pytest.raises(ContractCandidateError, match="object version"):
        read_candidate(candidate_setup)

    assert candidate_setup[4].closed


@pytest.mark.parametrize(
    "field",
    [
        "source",
        "database_name",
        "schema_name",
        "table_name",
        "contract_version",
    ],
)
def test_identity_or_version_mismatch_is_rejected(
    candidate_setup, field
):
    contract = dict(candidate_setup[1])
    contract[field] = "different"
    raw = json.dumps(contract).encode("utf-8")
    replacement = io.BytesIO(raw)
    candidate_setup[5].get_object.return_value["Body"] = replacement
    reference = replace(
        candidate_setup[3],
        sha256=hashlib.sha256(raw).hexdigest(),
    )

    with pytest.raises(ContractCandidateError, match=field):
        read_candidate(candidate_setup, reference=reference)

    assert replacement.closed
    candidate_setup[4].close()


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json",
        b'{"source":"first","source":"second"}',
        b'{"value":NaN}',
        b"[]",
    ],
)
def test_invalid_candidate_document_is_rejected(
    candidate_setup, raw
):
    replacement = io.BytesIO(raw)
    candidate_setup[5].get_object.return_value["Body"] = replacement
    reference = replace(
        candidate_setup[3],
        sha256=hashlib.sha256(raw).hexdigest(),
    )

    with pytest.raises(ContractCandidateError):
        read_candidate(candidate_setup, reference=reference)

    assert replacement.closed
    candidate_setup[4].close()


def test_candidate_size_is_bounded(candidate_setup):
    candidate_setup[-1].max_bytes = 10

    with pytest.raises(ContractCandidateError, match="size limit"):
        read_candidate(candidate_setup)

    assert candidate_setup[4].closed


def test_access_failure_propagates(candidate_setup):
    error = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "Denied"}},
        "GetObject",
    )
    candidate_setup[5].get_object.side_effect = error

    with pytest.raises(ClientError) as raised:
        read_candidate(candidate_setup)

    assert raised.value is error
    candidate_setup[4].close()