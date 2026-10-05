import copy
import importlib
import io
import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError


os.environ["AUDIT_TABLE_NAME"] = "unit-test-contracts"
os.environ["AWS_DEFAULT_REGION"] = "eu-west-2"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"

main = importlib.import_module("main")


def contract(version="v1"):
    return {
        "type": "record",
        "name": "Example",
        "fields": [{"name": "id", "type": "long"}],
        "source": "example-service",
        "database_name": "example_db",
        "schema_name": "public",
        "table_name": "example_table",
        "contract_version": version,
    }


def record(key="contracts/example/v1/contract.avsc", version_id=None):
    object_details = {"key": key}
    if version_id is not None:
        object_details["versionId"] = version_id

    return {
        "s3": {
            "bucket": {"name": "unit-test-bucket"},
            "object": object_details,
        }
    }


def aws_error(code, operation):
    return ClientError(
        {"Error": {"Code": code, "Message": "Test failure"}},
        operation,
    )


def registration_item(value):
    return {
        "contract_id": main.build_contract_id(value),
        "contract_version": value["contract_version"],
        "schema_fingerprint": main.calculate_contract_fingerprint(value),
        "s3_contract_uri": "s3://unit-test-bucket/original.avsc",
        "registered_at": "original-timestamp",
        "is_active": False,
    }


@pytest.fixture
def aws(monkeypatch):
    s3 = Mock()
    table = Mock()
    monkeypatch.setattr(main, "s3", s3)
    monkeypatch.setattr(main, "audit_table", table)
    return s3, table


@pytest.fixture
def context():
    return SimpleNamespace(aws_request_id="test-execution")


def s3_response(value, version_id=None):
    response = {
        "Body": io.BytesIO(json.dumps(value).encode("utf-8"))
    }
    if version_id is not None:
        response["VersionId"] = version_id
    return response


def test_new_registration(aws, context):
    s3, table = aws
    value = contract()
    s3.get_object.return_value = s3_response(value)

    result = main.lambda_handler(
        {"Records": [record()]}, context
    )

    assert result["results"][0]["status"] == "REGISTERED"
    item = table.put_item.call_args.kwargs["Item"]
    assert item["source"] == value["source"]
    assert item["database_name"] == value["database_name"]
    assert item["contract_version"] == "v1"
    assert item["ingestion_version"] is None
    assert item["is_active"] is False
    assert item["registration_execution_id"] == "test-execution"


def test_duplicate_preserves_original_registration(aws, context):
    s3, table = aws
    value = contract()
    existing = registration_item(value)
    s3.get_object.return_value = s3_response(value)
    table.put_item.side_effect = aws_error(
        "ConditionalCheckFailedException", "PutItem"
    )
    table.get_item.return_value = {"Item": existing}

    result = main.process_record(record(), context)

    assert result["status"] == "ALREADY_REGISTERED"
    assert result["s3_contract_uri"] == existing["s3_contract_uri"]
    assert table.get_item.call_args.kwargs["ConsistentRead"] is True
    assert table.put_item.call_count == 1


def test_duplicate_does_not_skip_next_record(aws, context):
    s3, table = aws
    first = contract()
    second = contract()
    second["table_name"] = "second_table"

    s3.get_object.side_effect = [
        s3_response(first),
        s3_response(second),
    ]
    table.put_item.side_effect = [
        aws_error("ConditionalCheckFailedException", "PutItem"),
        {},
    ]
    table.get_item.return_value = {
        "Item": registration_item(first)
    }

    result = main.lambda_handler(
        {
            "Records": [
                record(),
                record("contracts/second/v1/contract.avsc"),
            ]
        },
        context,
    )

    assert [item["status"] for item in result["results"]] == [
        "ALREADY_REGISTERED",
        "REGISTERED",
    ]
    assert table.put_item.call_count == 2


def test_different_content_same_version_is_rejected(aws):
    _, table = aws
    original = registration_item(contract())
    changed = contract()
    changed["fields"].append({"name": "description", "type": "string"})

    table.put_item.side_effect = aws_error(
        "ConditionalCheckFailedException", "PutItem"
    )
    table.get_item.return_value = {"Item": original}

    with pytest.raises(ValueError, match="different content"):
        main.register_contract(registration_item(changed))


def test_versions_share_contract_id():
    assert main.build_contract_id(contract("v1")) == (
        main.build_contract_id(contract("v2"))
    )

    other = contract()
    other["table_name"] = "other_table"
    assert main.build_contract_id(other) != (
        main.build_contract_id(contract())
    )


@pytest.mark.parametrize("version", ["v1", "v2", "v10"])
def test_valid_versions(version):
    main.validate_required_metadata(contract(version))


@pytest.mark.parametrize(
    "version", ["v0", "V1", "v01", "v", "1", "v-1", "v1.0"]
)
def test_invalid_versions(version):
    with pytest.raises(ValueError, match="contract_version"):
        main.validate_required_metadata(contract(version))


@pytest.mark.parametrize(
    "field",
    ["source", "database_name", "schema_name", "table_name"],
)
def test_ambiguous_identity_is_rejected(field):
    value = contract()
    value[field] = "a:b"

    with pytest.raises(ValueError, match="must not contain"):
        main.validate_required_metadata(value)


@pytest.mark.parametrize("value", ["", None, 123, " example "])
def test_invalid_metadata(value):
    data = contract()
    data["source"] = value

    with pytest.raises(ValueError):
        main.validate_required_metadata(data)


def test_missing_metadata():
    data = contract()
    del data["database_name"]

    with pytest.raises(ValueError, match="database_name"):
        main.validate_required_metadata(data)


def test_missing_existing_record(aws):
    _, table = aws
    table.put_item.side_effect = aws_error(
        "ConditionalCheckFailedException", "PutItem"
    )
    table.get_item.return_value = {}

    with pytest.raises(RuntimeError, match="could not be found"):
        main.register_contract(registration_item(contract()))


def test_missing_existing_fingerprint(aws):
    _, table = aws
    existing = registration_item(contract())
    del existing["schema_fingerprint"]
    table.put_item.side_effect = aws_error(
        "ConditionalCheckFailedException", "PutItem"
    )
    table.get_item.return_value = {"Item": existing}

    with pytest.raises(RuntimeError, match="no schema_fingerprint"):
        main.register_contract(registration_item(contract()))


def test_s3_failure_propagates_without_dynamodb_write(aws, context):
    s3, table = aws
    error = aws_error("AccessDenied", "GetObject")
    s3.get_object.side_effect = error

    with pytest.raises(ClientError) as raised:
        main.process_record(record(), context)

    assert raised.value is error
    table.put_item.assert_not_called()


def test_dynamodb_failure_propagates(aws, context):
    s3, table = aws
    s3.get_object.return_value = s3_response(contract())
    error = aws_error("AccessDeniedException", "PutItem")
    table.put_item.side_effect = error

    with pytest.raises(ClientError) as raised:
        main.process_record(record(), context)

    assert raised.value is error
    table.get_item.assert_not_called()


def test_reads_event_object_version_and_decodes_key(aws, context):
    s3, _ = aws
    s3.get_object.return_value = s3_response(
        contract(), version_id="object-version-123"
    )

    result = main.process_record(
        record(
            key="contracts%2Fexample+contract.avsc",
            version_id="object-version-123",
        ),
        context,
    )

    s3.get_object.assert_called_once_with(
        Bucket="unit-test-bucket",
        Key="contracts/example contract.avsc",
        VersionId="object-version-123",
    )
    assert result["s3_object_version_id"] == "object-version-123"


def test_invalid_json_closes_body_and_does_not_write(aws, context):
    s3, table = aws
    body = io.BytesIO(b"not-json")
    s3.get_object.return_value = {"Body": body}

    with pytest.raises(json.JSONDecodeError):
        main.process_record(record(), context)

    assert body.closed
    table.put_item.assert_not_called()


def test_fingerprint_ignores_object_key_order():
    value = contract()
    reordered = dict(reversed(list(value.items())))

    assert main.calculate_contract_fingerprint(value) == (
        main.calculate_contract_fingerprint(reordered)
    )

    changed = copy.deepcopy(value)
    changed["fields"][0]["type"] = "string"
    assert main.calculate_contract_fingerprint(value) != (
        main.calculate_contract_fingerprint(changed)
    )


@pytest.mark.parametrize("event", [None, {}, {"Records": []}])
def test_invalid_event(event, context):
    with pytest.raises(ValueError):
        main.lambda_handler(event, context)