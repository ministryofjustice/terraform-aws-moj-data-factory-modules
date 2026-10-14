import copy
import importlib.util
import io
import json
import os
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import pyarrow as pa
import pyarrow.parquet as pq
from botocore.exceptions import ClientError


os.environ["VALIDATION_AUDIT_TABLE_NAME"] = "unit-test-validation"
os.environ["SCHEMA_AUDIT_TABLE_NAME"] = "unit-test-contracts"
os.environ["INGESTION_TYPE"] = "CDC"
os.environ["AWS_DEFAULT_REGION"] = "eu-west-2"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"

spec = importlib.util.spec_from_file_location(
    "schema_validation_under_test",
    Path(__file__).with_name("main.py"),
)
main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main)


def identity():
    return {
        "source": "sandbox-oracle",
        "database_name": "DMSTEST",
        "schema_name": "DMS_USER",
        "table_name": "DMS_INTEGRATION_TEST",
    }


def contract(version="v1", scale=0):
    return {
        "type": "record",
        "name": "DMS_INTEGRATION_TEST",
        "namespace": "uk.gov.justice.contracts",
        **identity(),
        "contract_version": version,
        "fields": [
            {
                "name": "ID",
                "type": {
                    "type": "bytes",
                    "logicalType": "decimal",
                    "precision": 19,
                    "scale": scale,
                },
            }
        ],
    }


def record(version_id="data-version-1"):
    return {
        "s3": {
            "bucket": {"name": "incoming-bucket"},
            "object": {
                "key": "landing/DMS_USER/DMS_INTEGRATION_TEST/file.parquet",
                "versionId": version_id,
                "eTag": "data-etag",
                "sequencer": "001",
            },
        }
    }


def registry_item(version="v1"):
    value = contract(version)
    return {
        "contract_id": main.build_contract_id(value),
        "contract_version": version,
        "s3_contract_uri": (
            "s3://contracts-bucket/sandbox-oracle/DMSTEST/"
            f"DMS_USER/DMS_INTEGRATION_TEST/{version}/contract.avsc"
        ),
        "s3_object_version_id": "contract-version-1",
    }


def parquet_bytes(scale=0):
    decimal_type = pa.decimal128(19, scale)
    schema = pa.schema([
        pa.field("ID", decimal_type, nullable=False)
    ])
    table = pa.Table.from_arrays(
        [pa.array([Decimal("1")], type=decimal_type)],
        schema=schema,
    )
    output = io.BytesIO()
    pq.write_table(table, output)
    return output.getvalue()


def aws_error(code, operation):
    return ClientError(
        {"Error": {"Code": code, "Message": "Test failure"}},
        operation,
    )


@pytest.fixture
def context():
    return SimpleNamespace(aws_request_id="test-execution")


@pytest.fixture
def aws(monkeypatch):
    s3 = Mock()
    registry = Mock()
    audit = Mock()

    monkeypatch.setattr(main, "s3", s3)
    monkeypatch.setattr(main, "schema_audit_table", registry)
    monkeypatch.setattr(main, "validation_audit_table", audit)

    monkeypatch.setenv(
        "SOURCE_MAPPINGS",
        json.dumps([
            {
                "bucket_name": "incoming-bucket",
                "prefix": "landing/DMS_USER/DMS_INTEGRATION_TEST/",
                **identity(),
            }
        ]),
    )

    registry.query.return_value = {
        "Items": [registry_item()]
    }

    return s3, registry, audit


def configure_reads(s3, scale=0, registered=None):
    data = parquet_bytes(scale)
    registered = contract() if registered is None else registered

    s3.head_object.return_value = {
        "ContentLength": len(data),
        "ETag": '"data-etag"',
        "VersionId": "data-version-1",
    }

    def get_object(**request):
        if request["Bucket"] == "contracts-bucket":
            assert request["VersionId"] == "contract-version-1"
            return {
                "Body": io.BytesIO(
                    json.dumps(registered).encode("utf-8")
                ),
                "VersionId": "contract-version-1",
            }

        assert request["Bucket"] == "incoming-bucket"
        assert request["VersionId"] == "data-version-1"
        assert request["IfMatch"] == '"data-etag"'

        start, end = map(
            int,
            request["Range"].removeprefix("bytes=").split("-"),
        )
        return {
            "Body": io.BytesIO(data[start:end + 1])
        }

    s3.get_object.side_effect = get_object


@pytest.mark.parametrize("ingestion_type", ["FULL", "CDC"])
def test_matching_file(aws, context, monkeypatch, ingestion_type):
    s3, _, audit = aws
    monkeypatch.setattr(main, "INGESTION_TYPE", ingestion_type)
    configure_reads(s3)

    result = main.lambda_handler(
        {"Records": [record()]},
        context,
    )

    assert result["processed_records"] == 1
    outcome = result["results"][0]
    assert outcome["validation_status"] == "SUCCESS"
    assert outcome["comparison_outcome"] == "NO_CHANGE"
    assert (
        outcome["schema_fingerprint"]
        == outcome["registered_schema_fingerprint"]
    )
    assert "ingestion_version" not in outcome

    item = audit.put_item.call_args.kwargs["Item"]
    assert item["validation_id"] == outcome["validation_id"]
    assert item["ingestion_type"] == ingestion_type
    assert item["s3_object_version_id"] == "data-version-1"
    assert (
        item["registered_contract_object_version_id"]
        == "contract-version-1"
    )
    s3.head_object.assert_called_once_with(
        Bucket="incoming-bucket",
        Key="landing/DMS_USER/DMS_INTEGRATION_TEST/file.parquet",
        VersionId="data-version-1",
        IfMatch='"data-etag"',
    )


def test_decimal_scale_mismatch_is_audited(aws, context):
    s3, _, audit = aws
    configure_reads(s3, scale=2)

    with pytest.raises(main.SchemaValidationFailure) as raised:
        main.process_event(record(), context)

    assert raised.value.error_type == "SCHEMA_MISMATCH"
    item = audit.put_item.call_args.kwargs["Item"]
    assert item["validation_status"] == "FAIL"
    assert item["comparison_outcome"] == "SCHEMA_CHANGED"
    assert item["schema_differences"]
    assert (
        item["schema_fingerprint"]
        != item["registered_schema_fingerprint"]
    )


def test_missing_contract_is_audited(aws, context):
    s3, registry, audit = aws
    registry.query.return_value = {"Items": []}

    with pytest.raises(main.SchemaValidationFailure) as raised:
        main.process_event(record(), context)

    assert raised.value.error_type == "CONTRACT_NOT_FOUND"
    item = audit.put_item.call_args.kwargs["Item"]
    assert item["error_type"] == "CONTRACT_NOT_FOUND"
    assert item["comparison_outcome"] == "NOT_COMPLETED"
    s3.get_object.assert_not_called()


def test_s3_failure_is_audited_and_raised(aws, context):
    s3, _, audit = aws
    error = aws_error("AccessDenied", "GetObject")
    s3.get_object.side_effect = error

    with pytest.raises(ClientError) as raised:
        main.process_event(record(), context)

    assert raised.value is error
    item = audit.put_item.call_args.kwargs["Item"]
    assert item["validation_status"] == "FAIL"
    assert item["error_type"] == "VALIDATION_PROCESSING_ERROR"


def test_audit_failure_does_not_return_success(aws, context):
    s3, _, audit = aws
    configure_reads(s3)
    error = aws_error("AccessDeniedException", "PutItem")
    audit.put_item.side_effect = error

    with pytest.raises(ClientError) as raised:
        main.process_event(record(), context)

    assert raised.value is error


def test_unmapped_file_is_audited(aws, context):
    s3, registry, audit = aws
    event_record = record()
    event_record["s3"]["object"]["key"] = "other/file.parquet"

    with pytest.raises(ValueError, match="source mapping"):
        main.process_event(event_record, context)

    item = audit.put_item.call_args.kwargs["Item"]
    assert item["validation_status"] == "FAIL"
    registry.query.assert_not_called()
    s3.get_object.assert_not_called()


def test_contract_identity_mismatch_is_rejected(aws, context):
    s3, _, audit = aws
    registered = contract()
    registered["source"] = "another-source"
    configure_reads(s3, registered=registered)

    with pytest.raises(main.SchemaValidationFailure) as raised:
        main.process_event(record(), context)

    assert raised.value.error_type == "CONTRACT_IDENTITY_MISMATCH"
    assert (
        audit.put_item.call_args.kwargs["Item"]["error_type"]
        == "CONTRACT_IDENTITY_MISMATCH"
    )
    s3.head_object.assert_not_called()


def test_contract_version_mismatch_is_rejected(aws, context):
    s3, _, audit = aws
    configure_reads(s3, registered=contract("v2"))

    with pytest.raises(main.SchemaValidationFailure) as raised:
        main.process_event(record(), context)

    assert raised.value.error_type == "CONTRACT_VERSION_MISMATCH"
    assert (
        audit.put_item.call_args.kwargs["Item"]["validation_status"]
        == "FAIL"
    )


def test_retry_uses_same_validation_id(aws, context):
    s3, _, audit = aws
    configure_reads(s3)

    first = main.process_event(record(), context)
    second = main.process_event(record(), context)

    assert first["validation_id"] == second["validation_id"]
    written_ids = [
        call.kwargs["Item"]["validation_id"]
        for call in audit.put_item.call_args_list
    ]
    assert len(set(written_ids)) == 1


def test_different_object_versions_have_different_ids():
    first = record()["s3"]["object"]
    second = record("data-version-2")["s3"]["object"]

    assert main.build_validation_id(
        "incoming-bucket", first["key"], first
    ) != main.build_validation_id(
        "incoming-bucket", second["key"], second
    )


def test_unversioned_event_requires_identity_details():
    details = {"eTag": "example"}

    with pytest.raises(ValueError, match="eTag and sequencer"):
        main.build_validation_id("bucket", "file.parquet", details)


def test_latest_contract_reads_all_pages(aws):
    _, registry, _ = aws
    registry.query.side_effect = [
        {
            "Items": [
                registry_item("v2"),
                registry_item("v9"),
            ],
            "LastEvaluatedKey": {
                "contract_id": "example",
                "contract_version": "v9",
            },
        },
        {"Items": [registry_item("v10")]},
    ]

    selected = main.get_latest_contract(
        main.build_contract_id(identity())
    )

    assert selected["contract_version"] == "v10"
    assert registry.query.call_count == 2
    assert registry.query.call_args_list[0].kwargs["ConsistentRead"]
    assert registry.query.call_args_list[1].kwargs[
        "ExclusiveStartKey"
    ] == {
        "contract_id": "example",
        "contract_version": "v9",
    }


@pytest.mark.parametrize("version", ["v0", "v01", "V1", " v1", "v1 "])
def test_invalid_contract_versions(version):
    with pytest.raises(ValueError):
        main.extract_version_number(version)


def schema(field_type):
    return {
        "type": "record",
        "name": "Example",
        "namespace": "example.validation",
        "fields": [{"name": "value", "type": field_type}],
    }


@pytest.mark.parametrize(
    "expected,actual",
    [
        (
            {"type": "bytes", "logicalType": "decimal",
             "precision": 19, "scale": 0},
            {"type": "bytes", "logicalType": "decimal",
             "precision": 20, "scale": 0},
        ),
        (
            {"type": "bytes", "logicalType": "decimal",
             "precision": 19, "scale": 0},
            {"type": "bytes", "logicalType": "decimal",
             "precision": 19, "scale": 2},
        ),
        (
            {"type": "array", "items": "string"},
            {"type": "array", "items": "long"},
        ),
        (
            {"type": "record", "name": "Nested",
             "fields": [{"name": "child", "type": "string"}]},
            {"type": "record", "name": "Nested",
             "fields": [{"name": "child", "type": "long"}]},
        ),
        (
            {"type": "long", "logicalType": "timestamp-millis"},
            {"type": "long", "logicalType": "timestamp-micros"},
        ),
        ("string", ["null", "string"]),
    ],
)
def test_schema_changes_affect_fingerprint(expected, actual):
    assert main.generate_schema_fingerprint(schema(expected)) != (
        main.generate_schema_fingerprint(schema(actual))
    )


def test_contract_metadata_does_not_affect_comparison():
    original = schema("string")
    changed = copy.deepcopy(original)
    changed.update({
        "source": "another-service",
        "namespace": "another.namespace",
        "contract_version": "v2",
        "contacts": {"owner": "example"},
    })

    assert main.generate_schema_fingerprint(original) == (
        main.generate_schema_fingerprint(changed)
    )


@pytest.mark.parametrize("arrow_type", [pa.uint64(), pa.timestamp("ns")])
def test_unsupported_arrow_types_are_rejected(arrow_type):
    with pytest.raises(ValueError):
        main.arrow_type_to_avro(arrow_type, "example")


def test_array_element_nullability_is_preserved():
    nullable = pa.list_(
        pa.field("item", pa.string(), nullable=True)
    )
    required = pa.list_(
        pa.field("item", pa.string(), nullable=False)
    )

    assert main.arrow_type_to_avro(nullable, "example")["items"] == [
        "null", "string"
    ]
    assert main.arrow_type_to_avro(required, "example")["items"] == "string"


def test_handler_stops_after_a_failed_record(monkeypatch, context):
    processor = Mock(side_effect=ValueError("Test failure"))
    monkeypatch.setattr(main, "process_event", processor)

    with pytest.raises(RuntimeError, match="Processing stopped"):
        main.lambda_handler(
            {"Records": [record(), record("data-version-2")]},
            context,
        )

    assert processor.call_count == 1


@pytest.mark.parametrize("event", [None, {}, {"Records": []}])
def test_invalid_event(event, context):
    with pytest.raises(ValueError):
        main.lambda_handler(event, context)