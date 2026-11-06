import hashlib
import io
import json
import uuid
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from ingestion_registry.contract_verification import (
    ContractNotRegistered,
    ContractVerificationError,
    RegisteredContractVerifier,
)
from ingestion_registry.identity import TableIdentity


@pytest.fixture
def setup():
    identity = TableIdentity(
        environment="development",
        source="sandbox-postgres",
        database_name="dms_test",
        schema_name="public",
        table_name="dms_integration_test",
    )

    contract = {
        "source": identity.source,
        "database_name": identity.database_name,
        "schema_name": identity.schema_name,
        "table_name": identity.table_name,
        "contract_version": "v1",
        "type": "record",
        "name": identity.table_name,
        "fields": [{"name": "id", "type": "long"}],
    }

    normalised = json.dumps(
        contract,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")

    raw = json.dumps(contract, indent=2).encode("utf-8")

    contract_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_DNS,
            "sandbox-postgres:dms_test:public:dms_integration_test",
        )
    )
    key = (
        "sandbox-postgres/dms_test/public/"
        "dms_integration_test/v1/contract.avsc"
    )

    item = {
        **{
            field: contract[field]
            for field in (
                "source",
                "database_name",
                "schema_name",
                "table_name",
                "contract_version",
            )
        },
        "contract_id": contract_id,
        "schema_fingerprint": hashlib.sha256(normalised).hexdigest(),
        "s3_contract_uri": f"s3://example-contracts/{key}",
        "s3_object_version_id": "object-version-one",
        "is_active": False,
    }

    table = Mock()
    table.get_item.return_value = {"Item": item}

    body = io.BytesIO(raw)
    s3 = Mock()
    s3.get_object.return_value = {
        "Body": body,
        "VersionId": "object-version-one",
    }

    verifier = RegisteredContractVerifier(
        contract_table=table,
        s3_client=s3,
        bucket_name="example-contracts",
        bucket_owner_account_id="123456789012",
    )

    return identity, item, table, s3, body, raw, verifier


def verify(setup):
    return setup[-1].verify(
        identity=setup[0],
        contract_version="v1",
    )


def test_verifies_exact_registered_object(setup):
    _, item, table, s3, body, raw, _ = setup

    result = verify(setup)

    table.get_item.assert_called_once_with(
        Key={
            "contract_id": item["contract_id"],
            "contract_version": "v1",
        },
        ConsistentRead=True,
    )
    s3.get_object.assert_called_once_with(
        Bucket="example-contracts",
        Key=(
            "sandbox-postgres/dms_test/public/"
            "dms_integration_test/v1/contract.avsc"
        ),
        VersionId="object-version-one",
        ExpectedBucketOwner="123456789012",
    )
    assert result.contract_fingerprint == item["schema_fingerprint"]
    assert result.object_reference.sha256 == hashlib.sha256(raw).hexdigest()
    assert result.object_reference.sha256 != result.contract_fingerprint
    assert body.closed


def test_verification_does_not_activate_or_write_registration(setup):
    _, item, table, _, _, _, _ = setup

    verify(setup)

    assert item["is_active"] is False
    assert [call[0] for call in table.method_calls] == ["get_item"]


def test_missing_registration_does_not_read_s3(setup):
    _, _, table, s3, _, _, _ = setup
    table.get_item.return_value = {}

    with pytest.raises(ContractNotRegistered):
        verify(setup)

    s3.get_object.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("source", "another-source"),
        ("contract_id", str(uuid.UUID(int=0))),
        ("contract_version", "v2"),
        ("s3_contract_uri", "s3://untrusted-bucket/contract.avsc"),
        ("s3_object_version_id", None),
        ("s3_object_version_id", "null"),
        ("schema_fingerprint", "invalid"),
    ],
)
def test_invalid_registration_is_rejected_before_s3(setup, field, value):
    _, item, _, s3, _, _, _ = setup
    item[field] = value

    with pytest.raises(ContractVerificationError):
        verify(setup)

    s3.get_object.assert_not_called()


def test_content_conflict_is_rejected(setup):
    setup[1]["schema_fingerprint"] = "a" * 64

    with pytest.raises(ContractVerificationError, match="fingerprint"):
        verify(setup)

    assert setup[4].closed


def test_object_identity_mismatch_is_rejected(setup):
    _, _, _, s3, body, raw, _ = setup
    contract = json.loads(raw)
    contract["table_name"] = "another_table"
    replacement = io.BytesIO(json.dumps(contract).encode("utf-8"))
    s3.get_object.return_value["Body"] = replacement

    with pytest.raises(ContractVerificationError, match="table_name"):
        verify(setup)

    assert replacement.closed
    body.close()


def test_wrong_object_version_is_rejected_and_body_closed(setup):
    setup[3].get_object.return_value["VersionId"] = "different-version"

    with pytest.raises(ContractVerificationError, match="object version"):
        verify(setup)

    assert setup[4].closed


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json",
        b'{"source":"first","source":"second"}',
        b'{"value":NaN}',
    ],
)
def test_invalid_json_is_rejected_and_body_closed(setup, raw):
    replacement = io.BytesIO(raw)
    setup[3].get_object.return_value["Body"] = replacement

    with pytest.raises(ContractVerificationError):
        verify(setup)

    assert replacement.closed
    setup[4].close()


def test_s3_failure_is_not_reported_as_missing_registration(setup):
    error = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "Denied"}},
        "GetObject",
    )
    setup[3].get_object.side_effect = error

    with pytest.raises(ClientError) as raised:
        verify(setup)

    assert raised.value is error