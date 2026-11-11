import copy
import importlib.util
import io
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from botocore.exceptions import ReadTimeoutError

from ingestion_registry.contract_assessment import (
    ContractAssessmentBlocked,
)
from ingestion_registry.contract_validation import ContractValidator

from test_contract_orchestration_generation_integration import (
    connected_orchestration as connected_orchestration,
    prepare,
)
from test_contract_preparation_integration import (
    preparation_setup as preparation_setup,
)
from test_contract_generation_completion_integration import (
    generation_completion as generation_completion,
)
from test_candidate_generation_integration import (
    candidate_generation as candidate_generation,
    generation_adapter as generation_adapter,
)
from test_contract_orchestration_integration import approve_request


FUNCTION_ARN = (
    "arn:aws:lambda:eu-west-2:123456789012:"
    "function:contract-validation:9"
)


def load_runtime_module(name, path, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, path)

    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load validation runtime: {path}")

    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(__import__("sys").modules, name, module)
    spec.loader.exec_module(module)

    return module


@pytest.fixture
def validated_orchestration(connected_orchestration, monkeypatch):
    setup = connected_orchestration
    coordinator = setup["coordinator"]

    runtime = (
        Path(__file__).resolve().parents[2]
        / "modules"
        / "data-contract-management"
        / "runtime"
        / "schema-validation"
    )

    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-2")
    monkeypatch.setenv(
        "VALIDATION_AUDIT_TABLE_NAME",
        "test-validation-audit",
    )
    monkeypatch.setenv(
        "SCHEMA_AUDIT_TABLE_NAME",
        setup["registration_table"].name,
    )
    monkeypatch.setenv("INGESTION_TYPE", "FULL")

    configuration = {
        "assessor_revision": "test-assessor-v1",
        "candidate": {
            "bucket_name": "test-contract-candidates",
            "prefix": "candidates/",
            "bucket_owner_account_id": "123456789012",
        },
        "baseline": {
            "bucket_name": "test-approved-contracts",
            "prefix": f"{setup['table'].source}/",
            "bucket_owner_account_id": "123456789012",
        },
        "evidence": {
            "bucket_name": "test-contract-evidence",
            "prefix": "validation/",
            "bucket_owner_account_id": "123456789012",
            "kms_key_arn": setup["kms_key"],
        },
        "read_kms_key_arns": [setup["kms_key"]],
    }
    monkeypatch.setenv(
        "CONTRACT_ASSESSMENT_CONFIG",
        json.dumps(configuration),
    )

    load_runtime_module(
        "logger",
        runtime / "logger.py",
        monkeypatch,
    )
    load_runtime_module(
        "contract_compatibility",
        runtime / "contract_compatibility.py",
        monkeypatch,
    )
    load_runtime_module(
        "contract_assessment",
        runtime / "contract_assessment.py",
        monkeypatch,
    )
    handler = load_runtime_module(
        "_contract_validation_integration_runtime",
        runtime / "main.py",
        monkeypatch,
    )
    monkeypatch.setattr(handler, "s3", setup["s3"])

    outputs = []

    def invoke_handler(**kwargs):
        assert kwargs["FunctionName"] == FUNCTION_ARN
        assert kwargs["InvocationType"] == "RequestResponse"

        event = json.loads(kwargs["Payload"])
        result = handler.lambda_handler(event, None)
        outputs.append(copy.deepcopy(result))

        return {
            "StatusCode": 200,
            "ExecutedVersion": "9",
            "Payload": io.BytesIO(
                json.dumps(result).encode("utf-8")
            ),
        }

    invocation = Mock()
    invocation.invoke.side_effect = invoke_handler

    validator = ContractValidator(
        assessment_requests=coordinator.assessment_requests,
        lambda_client=invocation,
        validation_function_arn=FUNCTION_ARN,
    )

    monkeypatch.setattr(coordinator, "validator", validator)

    return {
        **setup,
        "validation_invocation": invocation,
        "invoke_validation_handler": invoke_handler,
        "validation_outputs": outputs,
    }


def assess(setup, prepared):
    return setup["coordinator"].assess(
        table=setup["table"],
        reservation_key=prepared["reservation_key"],
        preparation_fingerprint=prepared["preparation_fingerprint"],
    )


def test_actual_validation_evidence_requires_first_contract_review(
    validated_orchestration,
):
    setup = validated_orchestration
    prepared = prepare(setup)

    result = assess(setup, prepared)

    assert result["status"] == "REVIEW_REQUIRED"

    output = setup["validation_outputs"][0]
    assert output["validation"] == "VALID"
    assert output["compatibility"] == "FIRST_CONTRACT"

    request = setup["lifecycle"].read(
        table=setup["table"],
        request_id=prepared["request_id"],
    )
    assert request["assessment"]["evidence_reference"] == (
        output["evidence_reference"]
    )
    assert request["assessment"]["candidate_reference"] == (
        prepared["candidate_reference"]
    )
    assert setup["registration_table"].scan(
        ConsistentRead=True
    )["Count"] == 0
    assert setup["coordinator"].lookup.resolve(
        table=setup["table"]
    ) is None


def test_repeated_actual_validation_retains_exact_evidence(
    validated_orchestration,
):
    setup = validated_orchestration
    prepared = prepare(setup)

    first = assess(setup, prepared)
    second = assess(setup, prepared)

    assert second == first
    assert (
        setup["validation_outputs"][0]["evidence_reference"]
        == setup["validation_outputs"][1]["evidence_reference"]
    )

    request = setup["lifecycle"].read(
        table=setup["table"],
        request_id=prepared["request_id"],
    )
    assert int(request["revision"]) == 1


def test_actual_validation_then_approval_registers_and_activates(
    validated_orchestration,
):
    setup = validated_orchestration
    prepared = prepare(setup)

    waiting = assess(setup, prepared)
    assert waiting["status"] == "REVIEW_REQUIRED"

    request = setup["lifecycle"].read(
        table=setup["table"],
        request_id=prepared["request_id"],
    )
    approve_request(
        {
            **setup,
            "orchestration_request": request,
        }
    )

    ready = assess(setup, prepared)

    assert ready["status"] == "CONTRACT_READY"
    assert ready["action"] == "ACTIVATED"
    assert ready["selection"]["registered_contract"][
        "contract_version"
    ] == prepared["contract_version"]

    assert setup["registration_table"].scan(
        ConsistentRead=True
    )["Count"] == 1

    assert setup["coordinator"].resolve(
        table=setup["table"]
    )["selection"] == ready["selection"]

    assert assess(setup, prepared) == ready


def test_returned_checksum_is_independently_verified(
    validated_orchestration,
):
    setup = validated_orchestration
    prepared = prepare(setup)
    original = setup["invoke_validation_handler"]

    def invoke_with_wrong_checksum(**kwargs):
        response = original(**kwargs)
        stream = response["Payload"]

        try:
            result = json.loads(stream.read())
        finally:
            stream.close()

        result["evidence_reference"]["sha256"] = "0" * 64
        response["Payload"] = io.BytesIO(
            json.dumps(result).encode("utf-8")
        )
        return response

    setup["validation_invocation"].invoke.side_effect = (
        invoke_with_wrong_checksum
    )

    with pytest.raises(ContractAssessmentBlocked, match="checksum"):
        assess(setup, prepared)

    assert setup["lifecycle"].read(
        table=setup["table"],
        request_id=prepared["request_id"],
    ) is None
    assert setup["registration_table"].scan(
        ConsistentRead=True
    )["Count"] == 0


def test_lost_validation_response_recovers_same_stored_evidence(
    validated_orchestration,
):
    setup = validated_orchestration
    prepared = prepare(setup)
    original = setup["invoke_validation_handler"]

    def invoke_then_lose_response(**kwargs):
        response = original(**kwargs)
        response["Payload"].close()
        raise ReadTimeoutError(
            endpoint_url="https://lambda.eu-west-2.amazonaws.com"
        )

    setup["validation_invocation"].invoke.side_effect = (
        invoke_then_lose_response
    )

    with pytest.raises(ReadTimeoutError):
        assess(setup, prepared)

    assert setup["lifecycle"].read(
        table=setup["table"],
        request_id=prepared["request_id"],
    ) is None

    original_reference = (
        setup["validation_outputs"][0]["evidence_reference"]
    )

    setup["validation_invocation"].invoke.side_effect = original

    result = assess(setup, prepared)

    assert result["status"] == "REVIEW_REQUIRED"
    assert setup["validation_outputs"][1]["evidence_reference"] == (
        original_reference
    )