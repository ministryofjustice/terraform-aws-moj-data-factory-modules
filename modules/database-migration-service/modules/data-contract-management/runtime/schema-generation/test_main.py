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

def selected_table_request(config):
    return {
        "source": config["CONTRACT_SERVICE"],
        "database_name": config["DATABASE_NAME"],
        "schema_name": config["SCHEMA_NAME"],
        "table_name": config["TABLE_LIST"][0],
        "contract_version": config["CONTRACT_VERSION"],
    }


def test_empty_request_preserves_configured_tables(config):
    assert main.resolve_requested_tables({}, config) == [
        "first_table",
        "second_table",
    ]


def test_request_selects_only_one_configured_table(config):
    request = selected_table_request(config)
    request["table_name"] = "second_table"

    assert main.resolve_requested_tables(request, config) == [
        "second_table"
    ]


@pytest.mark.parametrize(
    "field",
    ["source", "database_name", "schema_name", "contract_version"],
)
def test_generation_rejects_different_configured_identity(config, field):
    request = selected_table_request(config)
    request[field] = "v2" if field == "contract_version" else "different"

    with pytest.raises(ValueError, match=field):
        main.resolve_requested_tables(request, config)


def test_generation_rejects_unconfigured_table(config):
    request = selected_table_request(config)
    request["table_name"] = "unconfigured_table"

    with pytest.raises(ValueError, match="TABLE_LIST"):
        main.resolve_requested_tables(request, config)


@pytest.mark.parametrize(
    "event",
    [
        None,
        [],
        {"table_name": "first_table"},
    ],
)
def test_generation_rejects_invalid_selection_request(config, event):
    with pytest.raises(ValueError):
        main.resolve_requested_tables(event, config)


def test_generation_rejects_connection_override(config):
    request = selected_table_request(config)
    request["DB_SECRET_ARN"] = "another-secret"

    with pytest.raises(ValueError, match="exactly"):
        main.resolve_requested_tables(request, config)


@pytest.mark.parametrize(
    "value",
    ["", " first_table", "first_table ", None, 123],
)
def test_generation_rejects_invalid_selected_table(config, value):
    request = selected_table_request(config)
    request["table_name"] = value

    with pytest.raises(ValueError, match="table_name"):
        main.resolve_requested_tables(request, config)


def test_invalid_selection_fails_before_database_access(
    monkeypatch, config
):
    secret_lookup = Mock()
    monkeypatch.setattr(
        main, "validate_configuration", lambda: config
    )
    monkeypatch.setattr(main, "get_database_secret", secret_lookup)

    request = selected_table_request(config)
    request["table_name"] = "unconfigured_table"

    with pytest.raises(ValueError, match="TABLE_LIST"):
        main.lambda_handler(
            request,
            SimpleNamespace(aws_request_id="test-execution"),
        )

    secret_lookup.assert_not_called()

def test_handler_generates_only_selected_table(monkeypatch, config):
    engine = Mock()
    connection = Mock()
    inspector = Mock()

    engine.connect.return_value.__enter__ = Mock(
        return_value=connection
    )
    engine.connect.return_value.__exit__ = Mock(
        return_value=False
    )

    generated = {
        **selected_table_request(config),
        "table_name": "second_table",
        "status": "STORED",
    }
    generation = Mock(return_value=generated)

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
        main, "inspect", lambda _: inspector
    )
    monkeypatch.setattr(
        main, "generate_contract", generation
    )

    request = selected_table_request(config)
    request["table_name"] = "second_table"

    result = main.lambda_handler(
        request,
        SimpleNamespace(aws_request_id="test-execution"),
    )

    generation.assert_called_once_with(
        inspector=inspector,
        engine_type=config["ENGINE"],
        database_name=config["DATABASE_NAME"],
        schema_name=config["SCHEMA_NAME"],
        table_name="second_table",
        service=config["CONTRACT_SERVICE"],
        schema_registry_bucket=config["SCHEMA_REGISTRY_BUCKET"],
        contract_version=config["CONTRACT_VERSION"],
        namespace=config["CONTRACT_NAMESPACE"],
        execution_id="test-execution",
        contacts=config["CONTRACT_CONTACTS"],
    )
    assert result["status"] == "SUCCEEDED"
    assert result["contracts"] == [generated]
    connection.rollback.assert_not_called()
    engine.dispose.assert_called_once()

@pytest.fixture
def candidate_environment(monkeypatch, config):
    destination = {
        "bucket_name": "example-candidates",
        "bucket_owner_account_id": "123456789012",
        "kms_key_arn": (
            "arn:aws:kms:eu-west-2:123456789012:"
            "key/11111111-2222-3333-4444-555555555555"
        ),
    }
    monkeypatch.setenv(
        "CONTRACT_CANDIDATE_BUCKET",
        destination["bucket_name"],
    )
    monkeypatch.setenv(
        "CONTRACT_CANDIDATE_BUCKET_OWNER",
        destination["bucket_owner_account_id"],
    )
    monkeypatch.setenv(
        "CONTRACT_CANDIDATE_KMS_KEY_ARN",
        destination["kms_key_arn"],
    )
    monkeypatch.setenv(
        "CONTRACT_CONTROL_TABLE", "test-contract-control"
    )
    monkeypatch.setenv("CONTRACT_ENVIRONMENT", "development")

    reservation = {
        "pk": "CONTRACTVERSIONREQUEST#" + "c" * 64,
        "sk": "META",
        "record_type": "CONTRACT_VERSION_RESERVATION",
        "request_id": "contract-request-one",
        "request_fingerprint": "d" * 64,
        "table_identity": {
            "environment": "development",
            "source": config["CONTRACT_SERVICE"],
            "database_name": config["DATABASE_NAME"],
            "schema_name": config["SCHEMA_NAME"],
            "table_name": config["TABLE_LIST"][0],
        },
        "contract_version": config["CONTRACT_VERSION"],
    }
    monkeypatch.setattr(
        main,
        "read_contract_version_reservation",
        Mock(return_value=reservation),
    )
    return destination


def candidate_request(config):
    return {
        **selected_table_request(config),
        "operation": "GENERATE_CANDIDATE",
        "request_id": "contract-request-one",
        "version_reservation": {
            "request_key": "CONTRACTVERSIONREQUEST#" + "c" * 64,
            "request_fingerprint": "d" * 64,
        },
    }

def test_candidate_request_resolves_trusted_destination(
    config, candidate_environment
):
    tables, destination = main.resolve_generation_request(
        candidate_request(config), config
    )

    assert tables == ["first_table"]
    assert destination == {
        **candidate_environment,
        "request_id": "contract-request-one",
    }


def test_legacy_request_requires_no_candidate_configuration(
    config, monkeypatch
):
    for name in (
        "CONTRACT_CANDIDATE_BUCKET",
        "CONTRACT_CANDIDATE_BUCKET_OWNER",
        "CONTRACT_CANDIDATE_KMS_KEY_ARN",
    ):
        monkeypatch.delenv(name, raising=False)

    tables, destination = main.resolve_generation_request({}, config)

    assert tables == config["TABLE_LIST"]
    assert destination is None


@pytest.mark.parametrize(
    "name",
    [
        "CONTRACT_CANDIDATE_BUCKET",
        "CONTRACT_CANDIDATE_BUCKET_OWNER",
        "CONTRACT_CANDIDATE_KMS_KEY_ARN",
    ],
)
def test_missing_candidate_configuration_fails_before_database_access(
    config, candidate_environment, monkeypatch, name
):
    secret_lookup = Mock()
    monkeypatch.delenv(name)
    monkeypatch.setattr(
        main, "validate_configuration", lambda: config
    )
    monkeypatch.setattr(
        main, "get_database_secret", secret_lookup
    )

    with pytest.raises(ValueError, match=name):
        main.lambda_handler(
            candidate_request(config),
            SimpleNamespace(aws_request_id="candidate-execution"),
        )

    secret_lookup.assert_not_called()


def test_candidate_request_cannot_override_destination(
    config, candidate_environment
):
    request = candidate_request(config)
    request["bucket_name"] = "untrusted-bucket"

    with pytest.raises(ValueError, match="exactly"):
        main.resolve_generation_request(request, config)


def test_candidate_generation_uses_candidate_storage(
    config, candidate_environment, monkeypatch
):
    engine = Mock()
    connection = Mock()
    engine.connect.return_value.__enter__ = Mock(
        return_value=connection
    )
    engine.connect.return_value.__exit__ = Mock(
        return_value=False
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
    monkeypatch.setattr(main, "inspect", lambda _: Mock())
    monkeypatch.setattr(
        main,
        "get_table_metadata",
        lambda *_: (
            [
                {
                    "name": "id",
                    "type": sql_types.Integer(),
                    "nullable": False,
                }
            ],
            ["id"],
        ),
    )

    candidate_reference = {
        "bucket_name": "example-candidates",
        "object_key": "candidates/example/contract.avsc",
        "version_id": "candidate-one",
        "sha256": "a" * 64,
    }
    store = Mock(
        return_value={
            "status": "CANDIDATE_STORED",
            "candidate_reference": candidate_reference,
        }
    )
    final_storage = Mock()

    monkeypatch.setattr(main, "store_candidate", store)
    monkeypatch.setattr(main, "save_contract", final_storage)

    result = main.lambda_handler(
        candidate_request(config),
        SimpleNamespace(aws_request_id="candidate-execution"),
    )

    final_storage.assert_not_called()
    store.assert_called_once()

    request = store.call_args.kwargs
    generated = json.loads(request["content"])

    assert request["bucket_name"] == "example-candidates"
    assert request["request_id"] == "contract-request-one"
    assert request["identity"]["table_name"] == "first_table"
    assert generated["table_name"] == "first_table"

    assert len(result["contracts"]) == 1
    candidate = result["contracts"][0]
    assert candidate["status"] == "CANDIDATE_STORED"
    assert candidate["candidate_reference"] == candidate_reference
    assert candidate["contract_fingerprint"] == (
        main.calculate_contract_fingerprint(generated)
    )
    assert "s3_contract_uri" not in candidate
    engine.dispose.assert_called_once()

def test_candidate_can_use_a_verified_reserved_version(
    config, candidate_environment
):
    request = candidate_request(config)
    request["contract_version"] = "v2"

    main.read_contract_version_reservation.return_value[
        "contract_version"
    ] = "v2"

    tables, destination = main.resolve_generation_request(
        request, config
    )

    assert tables == ["first_table"]
    assert destination["request_id"] == "contract-request-one"
    assert config["CONTRACT_VERSION"] == "v1"


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_id", "another-request"),
        ("request_fingerprint", "e" * 64),
        ("contract_version", "v2"),
        ("record_type", "INGESTION"),
    ],
)
def test_mismatched_reservation_blocks_database_access(
    config, candidate_environment, monkeypatch, field, value
):
    main.read_contract_version_reservation.return_value[field] = value
    secret_lookup = Mock()

    monkeypatch.setattr(
        main, "validate_configuration", lambda: config
    )
    monkeypatch.setattr(
        main, "get_database_secret", secret_lookup
    )

    with pytest.raises(ValueError, match="Version reservation"):
        main.lambda_handler(
            candidate_request(config),
            SimpleNamespace(aws_request_id="candidate-execution"),
        )

    secret_lookup.assert_not_called()


def test_missing_reservation_blocks_candidate_generation(
    config, candidate_environment
):
    main.read_contract_version_reservation.return_value = None

    with pytest.raises(ValueError, match="Version reservation"):
        main.resolve_generation_request(
            candidate_request(config), config
        )


def test_reservation_for_another_environment_is_rejected(
    config, candidate_environment
):
    main.read_contract_version_reservation.return_value[
        "table_identity"
    ]["environment"] = "production"

    with pytest.raises(ValueError, match="Version reservation"):
        main.resolve_generation_request(
            candidate_request(config), config
        )


def test_reservation_for_another_table_is_rejected(
    config, candidate_environment
):
    main.read_contract_version_reservation.return_value[
        "table_identity"
    ]["table_name"] = "another_table"

    with pytest.raises(ValueError, match="Version reservation"):
        main.resolve_generation_request(
            candidate_request(config), config
        )