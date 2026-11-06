import importlib.util
import json
import sys
import io

from botocore.exceptions import ReadTimeoutError

from ingestion_registry.contract_candidate import ContractCandidateReader
from ingestion_registry.contract_generation import ContractGenerator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import types as sql_types

from ingestion_registry.contract_versions import ContractVersions


RUNTIME_DIRECTORY = (
    Path(__file__).resolve().parents[2]
    / "modules"
    / "data-contract-management"
    / "runtime"
    / "schema-generation"
)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def candidate_generation(registration_integration, monkeypatch):
    setup = registration_integration
    table = setup["table"]

    logger_module = load_module(
        "candidate_generation_logger_under_test",
        RUNTIME_DIRECTORY / "logger.py",
    )
    storage_module = load_module(
        "candidate_generation_storage_under_test",
        RUNTIME_DIRECTORY / "candidate_storage.py",
    )

    with patch.dict(
        sys.modules,
        {
            "logger": logger_module,
            "candidate_storage": storage_module,
        },
    ):
        runtime = load_module(
            "candidate_generation_runtime_integration",
            RUNTIME_DIRECTORY / "main.py",
        )

    config = {
        "DB_SECRET_ARN": (
            "arn:aws:secretsmanager:eu-west-2:"
            "123456789012:secret:test"
        ),
        "SCHEMA_REGISTRY_BUCKET": "example-approved-contracts",
        "ENGINE": "postgres",
        "CONTRACT_NAMESPACE": "uk.gov.justice.contracts",
        "CONTRACT_SERVICE": table.source,
        "DATABASE_NAME": table.database_name,
        "SCHEMA_NAME": table.schema_name,
        "CONTRACT_VERSION": "v1",
        "TABLE_LIST": [table.table_name],
        "CONTRACT_CONTACTS": {},
    }

    for name, value in config.items():
        if name == "TABLE_LIST":
            value = ",".join(value)
        elif name == "CONTRACT_CONTACTS":
            value = json.dumps(value)

        monkeypatch.setenv(name, value)

    monkeypatch.delenv("DATABASE_SERVICE_NAME", raising=False)
    monkeypatch.delenv("DATABASE_SID", raising=False)

    monkeypatch.setenv(
        "CONTRACT_CONTROL_TABLE",
        setup["registry"].table_name,
    )
    monkeypatch.setenv(
        "CONTRACT_ENVIRONMENT",
        table.environment,
    )
    monkeypatch.setenv(
        "CONTRACT_CANDIDATE_BUCKET",
        setup["candidate"].bucket_name,
    )
    monkeypatch.setenv(
        "CONTRACT_CANDIDATE_BUCKET_OWNER",
        "123456789012",
    )
    monkeypatch.setenv(
        "CONTRACT_CANDIDATE_KMS_KEY_ARN",
        setup["kms_key"],
    )

    engine = Mock()
    connection = Mock()

    engine.connect.return_value.__enter__ = Mock(
        return_value=connection
    )
    engine.connect.return_value.__exit__ = Mock(
        return_value=False
    )

    secret_lookup = Mock(return_value={})

    monkeypatch.setattr(runtime, "s3", setup["s3"])
    monkeypatch.setattr(
        runtime,
        "get_database_secret",
        secret_lookup,
    )
    monkeypatch.setattr(
        runtime,
        "build_database_connection",
        lambda *_: None,
    )
    monkeypatch.setattr(
        runtime,
        "create_database_engine",
        lambda *_: engine,
    )
    monkeypatch.setattr(
        runtime,
        "inspect",
        lambda _: Mock(),
    )
    monkeypatch.setattr(
        runtime,
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

    versions = ContractVersions(
        registry=setup["registry"],
        registration_table=setup["registration_table"],
    )

    versions.reserve(
        table=table,
        request_id="earlier-generation-request",
        request_fingerprint="a" * 64,
    )

    reservation = versions.reserve(
        table=table,
        request_id="candidate-generation-request",
        request_fingerprint="b" * 64,
    )

    request = {
        "operation": "GENERATE_CANDIDATE",
        "request_id": reservation["request_id"],
        "source": table.source,
        "database_name": table.database_name,
        "schema_name": table.schema_name,
        "table_name": table.table_name,
        "contract_version": reservation["contract_version"],
        "version_reservation": {
            "request_key": reservation["pk"],
            "request_fingerprint": reservation[
                "request_fingerprint"
            ],
        },
    }

    return {
        **setup,
        "runtime": runtime,
        "generation_request": request,
        "reservation": reservation,
        "secret_lookup": secret_lookup,
        "engine": engine,
    }


def test_reserved_version_reaches_the_stored_candidate(
    candidate_generation,
):
    setup = candidate_generation
    runtime = setup["runtime"]

    assert setup["reservation"]["contract_version"] == "v2"
    assert runtime.validate_configuration()["CONTRACT_VERSION"] == "v1"

    result = runtime.lambda_handler(
        setup["generation_request"],
        SimpleNamespace(aws_request_id="generation-execution"),
    )

    assert result["status"] == "SUCCEEDED"
    assert len(result["contracts"]) == 1

    candidate = result["contracts"][0]
    reference = candidate["candidate_reference"]

    response = setup["s3"].get_object(
        Bucket=reference["bucket_name"],
        Key=reference["object_key"],
        VersionId=reference["version_id"],
    )

    body = response["Body"]
    try:
        stored_contract = json.loads(body.read())
    finally:
        body.close()

    assert stored_contract["contract_version"] == "v2"
    assert stored_contract["source"] == setup["table"].source
    assert stored_contract["database_name"] == (
        setup["table"].database_name
    )
    assert stored_contract["schema_name"] == setup["table"].schema_name
    assert stored_contract["table_name"] == setup["table"].table_name

    assert candidate["contract_fingerprint"] == (
        runtime.calculate_contract_fingerprint(stored_contract)
    )
    assert reference["object_key"].startswith("candidates/")
    assert reference["version_id"] != "null"
    assert "s3_contract_uri" not in candidate

    assert setup["registration_table"].scan(
        ConsistentRead=True
    )["Count"] == 0

    assert setup["registry"]._read(
        setup["table"].partition_key,
        "CONTRACT_CURRENT",
    ) is None

    assert runtime.validate_configuration()["CONTRACT_VERSION"] == "v1"
    setup["secret_lookup"].assert_called_once()
    setup["engine"].dispose.assert_called_once()


@pytest.mark.parametrize(
    "change",
    ["missing_reservation", "wrong_fingerprint", "wrong_version"],
)
def test_invalid_reservation_blocks_generation(
    candidate_generation,
    monkeypatch,
    change,
):
    setup = candidate_generation
    request = setup["generation_request"]

    if change == "missing_reservation":
        request["version_reservation"]["request_key"] = (
            "CONTRACTVERSIONREQUEST#" + "f" * 64
        )
    elif change == "wrong_fingerprint":
        request["version_reservation"]["request_fingerprint"] = (
            "c" * 64
        )
    else:
        request["contract_version"] = "v3"

    store = Mock()
    monkeypatch.setattr(
        setup["runtime"],
        "store_candidate",
        store,
    )

    with pytest.raises(ValueError, match="Version reservation"):
        setup["runtime"].lambda_handler(
            request,
            SimpleNamespace(aws_request_id="generation-execution"),
        )

    setup["secret_lookup"].assert_not_called()
    setup["engine"].connect.assert_not_called()
    store.assert_not_called()

@pytest.fixture
def generation_adapter(candidate_generation):
    setup = candidate_generation

    reader = ContractCandidateReader(
        s3_client=setup["s3"],
        candidate_bucket=setup["candidate"].bucket_name,
        candidate_prefix="candidates/",
        bucket_owner_account_id="123456789012",
    )

    # Track calls while retaining actual S3 content verification.
    tracked_reader = Mock(wraps=reader)
    invocation = Mock()

    function_arn = (
        "arn:aws:lambda:eu-west-2:123456789012:"
        "function:schema-generation:7"
    )

    def invoke_handler(**kwargs):
        assert kwargs["FunctionName"] == function_arn
        assert kwargs["InvocationType"] == "RequestResponse"

        result = setup["runtime"].lambda_handler(
            json.loads(kwargs["Payload"]),
            SimpleNamespace(
                aws_request_id="adapter-generation-execution"
            ),
        )

        return {
            "StatusCode": 200,
            "ExecutedVersion": "7",
            "Payload": io.BytesIO(
                json.dumps(result).encode("utf-8")
            ),
        }

    invocation.invoke.side_effect = invoke_handler

    generator = ContractGenerator(
        registry=setup["registry"],
        candidate_reader=tracked_reader,
        lambda_client=invocation,
        generation_function_arn=function_arn,
    )

    return {
        **setup,
        "generator": generator,
        "candidate_reader": tracked_reader,
        "invocation": invocation,
        "invoke_handler": invoke_handler,
        "function_arn": function_arn,
    }


def generate_through_adapter(setup):
    reservation = setup["reservation"]

    return setup["generator"].generate(
        table=setup["table"],
        reservation_key=reservation["pk"],
        request_fingerprint=reservation["request_fingerprint"],
    )

def test_adapter_verifies_candidate_from_actual_generation_handler(
    generation_adapter,
):
    setup = generation_adapter

    result = generate_through_adapter(setup)

    assert result["status"] == "CANDIDATE_VERIFIED"
    assert result["contract_version"] == "v2"
    assert result["request_id"] == (
        setup["reservation"]["request_id"]
    )
    assert result["reservation_key"] == (
        setup["reservation"]["pk"]
    )
    assert result["generation_function_arn"] == (
        setup["function_arn"]
    )

    setup["invocation"].invoke.assert_called_once()
    setup["candidate_reader"].read.assert_called_once()

    reference = result["candidate_reference"]
    response = setup["s3"].get_object(
        Bucket=reference["bucket_name"],
        Key=reference["object_key"],
        VersionId=reference["version_id"],
    )

    body = response["Body"]
    try:
        content = json.loads(body.read())
    finally:
        body.close()

    assert content["contract_version"] == "v2"
    assert content["table_name"] == setup["table"].table_name
    assert result["contract_fingerprint"] == (
        setup["runtime"].calculate_contract_fingerprint(content)
    )

    assert "content" not in result
    assert setup["registration_table"].scan(
        ConsistentRead=True
    )["Count"] == 0

    assert setup["registry"]._read(
        setup["table"].partition_key,
        "CONTRACT_CURRENT",
    ) is None


def test_lost_invocation_response_can_leave_a_stored_candidate(
    generation_adapter,
):
    setup = generation_adapter
    bucket = setup["candidate"].bucket_name

    def candidate_keys():
        response = setup["s3"].list_objects_v2(
            Bucket=bucket,
            Prefix="candidates/",
        )
        assert not response.get("IsTruncated", False)

        return {
            item["Key"]
            for item in response.get("Contents", [])
        }

    before = candidate_keys()

    def invoke_then_lose_response(**kwargs):
        response = setup["invoke_handler"](**kwargs)
        response["Payload"].close()

        raise ReadTimeoutError(
            endpoint_url="https://lambda.eu-west-2.amazonaws.com"
        )

    setup["invocation"].invoke.side_effect = (
        invoke_then_lose_response
    )

    with pytest.raises(ReadTimeoutError):
        generate_through_adapter(setup)

    created = candidate_keys() - before
    assert len(created) == 1

    setup["candidate_reader"].read.assert_not_called()

    reservation = setup["registry"]._read(
        setup["reservation"]["pk"],
        "META",
    )
    assert reservation == setup["reservation"]

    assert setup["registration_table"].scan(
        ConsistentRead=True
    )["Count"] == 0