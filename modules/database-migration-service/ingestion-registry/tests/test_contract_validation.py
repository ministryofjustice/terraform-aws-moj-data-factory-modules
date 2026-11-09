import copy
import io
import json
from decimal import Decimal
from unittest.mock import Mock

import pytest
from botocore.exceptions import ReadTimeoutError

from ingestion_registry.contract_validation import (
    ContractValidationFailed,
    ContractValidator,
)
from ingestion_registry.identity import TableIdentity
from ingestion_registry.references import S3ObjectReference


FUNCTION_ARN = (
    "arn:aws:lambda:eu-west-2:123456789012:"
    "function:contract-validation:7"
)


@pytest.fixture
def validation_setup():
    table = TableIdentity(
        environment="development",
        source="sandbox-postgres",
        database_name="dms_test",
        schema_name="public",
        table_name="example",
    )

    candidate = {
        "bucket_name": "test-contract-candidates",
        "object_key": "candidates/request-one/contract.avsc",
        "version_id": "candidate-version-one",
        "sha256": "a" * 64,
    }
    evidence = {
        "bucket_name": "test-contract-evidence",
        "object_key": "validation/request-one/result.json",
        "version_id": "evidence-version-one",
        "sha256": "b" * 64,
    }

    request = {
        "assessment_request_version": Decimal(1),
        "request_id": "request-one",
        "identity": {
            "source": table.source,
            "database_name": table.database_name,
            "schema_name": table.schema_name,
            "table_name": table.table_name,
            "contract_version": "v2",
        },
        "candidate_reference": candidate,
        "baseline_reference": None,
        "assessor_revision": "test-assessor-v1",
    }
    record = {"request": request}

    requests = Mock()
    requests.prepare.return_value = record
    requests.read.return_value = copy.deepcopy(record)

    result = {
        "status": "ASSESSMENT_COMPLETED",
        "request_id": "request-one",
        "evidence_reference": evidence,
        "validation": "VALID",
        "compatibility": "FIRST_CONTRACT",
        "reason": "NO_BASELINE",
        "ruleset_revision": "candidate-backward-v1",
    }

    client = Mock()

    def response():
        return {
            "StatusCode": 200,
            "ExecutedVersion": "7",
            "Payload": io.BytesIO(json.dumps(result).encode("utf-8")),
        }

    client.invoke.side_effect = lambda **kwargs: response()

    service = ContractValidator(
        assessment_requests=requests,
        lambda_client=client,
        validation_function_arn=FUNCTION_ARN,
    )

    return {
        "service": service,
        "requests": requests,
        "client": client,
        "request": request,
        "record": record,
        "result": result,
        "response": response,
        "arguments": {
            "table": table,
            "reservation_key": "CONTRACTVERSIONREQUEST#" + "c" * 64,
            "preparation_fingerprint": "d" * 64,
        },
    }


def assess(setup):
    return setup["service"].assess(**setup["arguments"])


def test_invokes_numeric_version_with_recorded_request(validation_setup):
    setup = validation_setup

    result = assess(setup)

    invocation = setup["client"].invoke.call_args.kwargs
    event = json.loads(invocation["Payload"])

    assert invocation["FunctionName"] == FUNCTION_ARN
    assert invocation["InvocationType"] == "RequestResponse"
    assert event["operation"] == "ASSESS_CONTRACT"
    assert event["request"] == setup["request"]
    assert type(event["request"]["assessment_request_version"]) is int

    assert result["status"] == "ASSESSMENT_EVIDENCE_RETURNED"
    assert result["evidence_reference"] == S3ObjectReference(
        **setup["result"]["evidence_reference"]
    )
    assert "compatibility" not in result

    setup["requests"].prepare.assert_called_once_with(
        **setup["arguments"]
    )
    setup["requests"].read.assert_called_once_with(
        **setup["arguments"]
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"StatusCode": 202},
        {"FunctionError": "Unhandled"},
        {"ExecutedVersion": "8"},
        {"Payload": None},
    ],
)
def test_failed_invocation_cannot_return_evidence(
    validation_setup,
    changes,
):
    setup = validation_setup
    response = setup["response"]()
    response.update(changes)
    setup["client"].invoke.side_effect = None
    setup["client"].invoke.return_value = response

    with pytest.raises(ContractValidationFailed):
        assess(setup)


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b"\xff",
        b'{"status":"ASSESSMENT_COMPLETED","status":"OTHER"}',
        b'{"value":NaN}',
        b"x" * 65537,
    ],
)
def test_invalid_response_is_rejected(validation_setup, raw):
    setup = validation_setup
    setup["client"].invoke.side_effect = None
    setup["client"].invoke.return_value = {
        "StatusCode": 200,
        "ExecutedVersion": "7",
        "Payload": io.BytesIO(raw),
    }

    with pytest.raises(ContractValidationFailed):
        assess(setup)


def test_wrong_request_id_is_rejected(validation_setup):
    setup = validation_setup
    setup["result"]["request_id"] = "another-request"

    with pytest.raises(ContractValidationFailed, match="selected"):
        assess(setup)


def test_unversioned_evidence_is_rejected(validation_setup):
    setup = validation_setup
    setup["result"]["evidence_reference"]["version_id"] = "null"

    with pytest.raises(
        ContractValidationFailed,
        match="evidence reference",
    ):
        assess(setup)


def test_changed_record_blocks_completion(validation_setup):
    setup = validation_setup
    changed = copy.deepcopy(setup["record"])
    changed["request"]["assessor_revision"] = "another-assessor"
    setup["requests"].read.return_value = changed

    with pytest.raises(ContractValidationFailed, match="changed"):
        assess(setup)


def test_timeout_propagates_for_retry(validation_setup):
    setup = validation_setup
    setup["client"].invoke.side_effect = ReadTimeoutError(
        endpoint_url="https://lambda.eu-west-2.amazonaws.com"
    )

    with pytest.raises(ReadTimeoutError):
        assess(setup)


def test_non_integral_dynamodb_number_is_not_serialised(
    validation_setup,
):
    setup = validation_setup
    setup["request"]["assessment_request_version"] = Decimal("1.5")

    with pytest.raises(TypeError):
        assess(setup)

    setup["client"].invoke.assert_not_called()


@pytest.mark.parametrize(
    "arn",
    [
        "arn:aws:lambda:eu-west-2:123456789012:function:validation",
        "arn:aws:lambda:eu-west-2:123456789012:function:validation:latest",
        "arn:aws:lambda:eu-west-2:123456789012:function:validation:$LATEST",
    ],
)
def test_requires_published_numeric_function_version(
    validation_setup,
    arn,
):
    setup = validation_setup

    with pytest.raises(ValueError, match="numeric"):
        ContractValidator(
            assessment_requests=setup["requests"],
            lambda_client=setup["client"],
            validation_function_arn=arn,
        )