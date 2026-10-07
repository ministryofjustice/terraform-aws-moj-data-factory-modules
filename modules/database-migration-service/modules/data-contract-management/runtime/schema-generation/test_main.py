import copy
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import types as sql_types
from sqlalchemy.dialects import oracle, postgresql


RUNTIME_DIRECTORY = Path(__file__).resolve().parent

spec = importlib.util.spec_from_file_location(
    "schema_generation_runtime_under_test",
    RUNTIME_DIRECTORY / "main.py",
)

main = importlib.util.module_from_spec(spec)

with (
    patch.object(
        sys,
        "path",
        [str(RUNTIME_DIRECTORY), *sys.path],
    ),
    patch("boto3.client"),
):
    spec.loader.exec_module(main)


@pytest.fixture
def config():
    return {
        "DB_SECRET_ARN": (
            "arn:aws:secretsmanager:eu-west-2:"
            "123456789012:secret:test"
        ),
        "SCHEMA_REGISTRY_BUCKET": "test-contracts",
        "ENGINE": "postgres",
        "CONTRACT_NAMESPACE": "uk.gov.justice.contracts",
        "CONTRACT_SERVICE": "example-service",
        "DATABASE_NAME": "example_db",
        "SCHEMA_NAME": "public",
        "CONTRACT_VERSION": "v1",
        "TABLE_LIST": ["first_table", "second_table"],
        "CONTRACT_CONTACTS": {},
    }


@pytest.fixture
def configured_environment(monkeypatch, config):
    for name, value in config.items():
        if name == "TABLE_LIST":
            value = ",".join(value)
        elif name == "CONTRACT_CONTACTS":
            value = json.dumps(value)

        monkeypatch.setenv(name, value)

    monkeypatch.delenv("DATABASE_SERVICE_NAME", raising=False)
    monkeypatch.delenv("DATABASE_SID", raising=False)

    return config


def contract():
    return main.build_avro_contract(
        namespace="uk.gov.justice.contracts",
        service="example-service",
        table_name="example_table",
        schema_name="public",
        database_name="example_db",
        engine_type="postgres",
        contract_version="v1",
        avro_fields=[
            {"name": "id", "type": "long", "key": "primary"}
        ],
        contacts={},
    )


def aws_error(code, status):
    return ClientError(
        {
            "Error": {
                "Code": code,
                "Message": "Test failure",
            },
            "ResponseMetadata": {
                "HTTPStatusCode": status,
            },
        },
        "PutObject",
    )


@pytest.fixture
def s3(monkeypatch):
    client = Mock()
    monkeypatch.setattr(main, "s3", client)
    return client


@pytest.mark.parametrize("version", ["v1", "v2", "v10"])
def test_valid_versions(
    configured_environment, monkeypatch, version
):
    monkeypatch.setenv("CONTRACT_VERSION", version)

    assert (
        main.validate_configuration()["CONTRACT_VERSION"]
        == version
    )


@pytest.mark.parametrize(
    "version",
    ["v0", "V1", "v01", "v", "1", "v-1", "v1.0"],
)
def test_invalid_versions(
    configured_environment, monkeypatch, version
):
    monkeypatch.setenv("CONTRACT_VERSION", version)

    with pytest.raises(ValueError, match="CONTRACT_VERSION"):
        main.validate_configuration()


def test_contacts_are_configured(
    configured_environment, monkeypatch
):
    contacts = {"team": "example-team", "slack": "#example"}

    monkeypatch.setenv(
        "CONTRACT_CONTACTS",
        json.dumps(contacts),
    )

    assert (
        main.validate_configuration()["CONTRACT_CONTACTS"]
        == contacts
    )


@pytest.mark.parametrize(
    "column_type, expected",
    [
        (postgresql.INTEGER(), "int"),
        (postgresql.BIGINT(), "long"),
        (postgresql.BOOLEAN(), "boolean"),
        (postgresql.REAL(), "float"),
        (postgresql.DOUBLE_PRECISION(), "double"),
        (postgresql.BYTEA(), "bytes"),
        (postgresql.TEXT(), "string"),
        (
            postgresql.TIMESTAMP(timezone=False),
            {
                "type": "long",
                "logicalType": "local-timestamp-micros",
            },
        ),
        (
            postgresql.TIMESTAMP(timezone=True),
            {
                "type": "long",
                "logicalType": "timestamp-micros",
            },
        ),
    ],
)
def test_postgres_mapping(column_type, expected):
    assert main.convert_postgres_type(column_type) == expected


@pytest.mark.parametrize(
    "column_type",
    [
        postgresql.ARRAY(sql_types.Integer()),
        postgresql.JSONB(),
        postgresql.ENUM("one", "two", name="example_enum"),
        postgresql.NUMERIC(),
    ],
)
def test_unsupported_postgres_types_fail(column_type):
    with pytest.raises(ValueError):
        main.convert_postgres_type(column_type)


@pytest.mark.parametrize(
    "column_type, expected",
    [
        (oracle.NUMBER(9, 0), "int"),
        (oracle.NUMBER(18, 0), "long"),
        (
            oracle.NUMBER(38, 0),
            {
                "type": "bytes",
                "logicalType": "decimal",
                "precision": 38,
                "scale": 0,
            },
        ),
        (
            oracle.NUMBER(12, 2),
            {
                "type": "bytes",
                "logicalType": "decimal",
                "precision": 12,
                "scale": 2,
            },
        ),
        (oracle.RAW(16), "bytes"),
        (oracle.BLOB(), "bytes"),
        (oracle.CLOB(), "string"),
    ],
)
def test_oracle_mapping(column_type, expected):
    assert main.convert_oracle_type(column_type) == expected


def test_unconstrained_oracle_number_fails():
    with pytest.raises(ValueError, match="explicit precision"):
        main.convert_oracle_type(oracle.NUMBER())


def test_nullable_field_has_matching_default():
    fields = main.build_avro_fields(
        [
            {
                "name": "description",
                "type": sql_types.String(),
                "nullable": True,
            }
        ],
        [],
        "postgres",
    )

    assert fields == [
        {
            "name": "description",
            "type": ["null", "string"],
            "default": None,
        }
    ]


def test_invalid_field_name_fails():
    with pytest.raises(ValueError, match="valid Avro name"):
        main.build_avro_fields(
            [
                {
                    "name": "invalid-name",
                    "type": sql_types.Integer(),
                    "nullable": False,
                }
            ],
            [],
            "postgres",
        )


def test_validation_does_not_modify_contract():
    value = contract()
    original = copy.deepcopy(value)

    main.validate_avro_contract(value)

    assert value == original


def test_new_contract_uses_conditional_write(s3):
    s3.put_object.return_value = {"VersionId": "version-123"}

    result = main.save_contract("test-contracts", contract())

    request = s3.put_object.call_args.kwargs

    assert request["IfNoneMatch"] == "*"
    assert request["Key"] == (
        "example-service/example_db/public/"
        "example_table/v1/contract.avsc"
    )
    assert result["status"] == "STORED"
    assert result["s3_object_version_id"] == "version-123"
    s3.get_object.assert_not_called()


def test_duplicate_returns_existing_version(s3):
    value = contract()
    body = io.BytesIO(json.dumps(value).encode("utf-8"))

    s3.put_object.side_effect = aws_error(
        "PreconditionFailed", 412
    )
    s3.get_object.return_value = {
        "Body": body,
        "VersionId": "original-version",
    }

    result = main.save_contract("test-contracts", value)

    assert result["status"] == "ALREADY_EXISTS"
    assert result["s3_object_version_id"] == "original-version"
    assert body.closed
    assert s3.put_object.call_count == 1


def test_conflicting_content_is_rejected(s3):
    original = contract()
    changed = copy.deepcopy(original)
    changed["description"] = "Different content"

    body = io.BytesIO(json.dumps(original).encode("utf-8"))

    s3.put_object.side_effect = aws_error(
        "PreconditionFailed", 412
    )
    s3.get_object.return_value = {"Body": body}

    with pytest.raises(ValueError, match="different content"):
        main.save_contract("test-contracts", changed)

    assert body.closed
    assert s3.put_object.call_count == 1


def test_aws_failure_propagates(s3):
    error = aws_error("AccessDenied", 403)
    s3.put_object.side_effect = error

    with pytest.raises(ClientError) as raised:
        main.save_contract("test-contracts", contract())

    assert raised.value is error
    s3.get_object.assert_not_called()


def test_invalid_contract_is_not_uploaded(s3):
    value = contract()
    del value["source"]

    with pytest.raises(ValueError):
        main.save_contract("test-contracts", value)

    s3.put_object.assert_not_called()


def test_services_have_different_paths(s3):
    first = contract()
    second = contract()
    second["source"] = "another-service"
    second["service"] = "another-service"

    first_result = main.save_contract("test-contracts", first)
    second_result = main.save_contract("test-contracts", second)

    assert first_result["s3_key"] != second_result["s3_key"]


def test_fingerprint_ignores_dictionary_key_order():
    value = contract()
    reordered = dict(reversed(list(value.items())))

    assert (
        main.calculate_contract_fingerprint(value)
        == main.calculate_contract_fingerprint(reordered)
    )


def test_connection_preserves_password(config):
    secret = {
        "username": "example_user",
        "password": "p@ss:/word?#%",
        "host": "database.example",
        "port": "5432",
    }

    url = main.build_database_connection(config, secret)

    assert url.password == secret["password"]
    assert url.drivername == "postgresql+psycopg2"
    assert url.database == config["DATABASE_NAME"]


def test_handler_reports_failure_and_attempts_next_table(
    monkeypatch, config
):
    engine = Mock()
    connection = Mock()

    engine.connect.return_value.__enter__ = Mock(
        return_value=connection
    )
    engine.connect.return_value.__exit__ = Mock(
        return_value=False
    )

    generation = Mock(
        side_effect=[
            ValueError("Unsupported source type"),
            {"status": "STORED"},
        ]
    )

    monkeypatch.setattr(
        main, "validate_configuration", lambda: config
    )
    monkeypatch.setattr(
        main, "get_database_secret", lambda _: {}
    )
    monkeypatch.setattr(
        main, "build_database_connection", lambda *_: None
    )
    monkeypatch.setattr(
        main, "create_database_engine", lambda *_: engine
    )
    monkeypatch.setattr(
        main, "inspect", lambda _: Mock()
    )
    monkeypatch.setattr(
        main, "generate_contract", generation
    )

    with pytest.raises(RuntimeError, match="first_table"):
        main.lambda_handler(
            {},
            SimpleNamespace(aws_request_id="test-execution"),
        )

    assert generation.call_count == 2
    connection.rollback.assert_called_once()
    engine.dispose.assert_called_once()