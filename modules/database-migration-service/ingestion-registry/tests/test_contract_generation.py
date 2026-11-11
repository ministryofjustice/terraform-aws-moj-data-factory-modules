import copy
import io
import json
from dataclasses import asdict
from unittest.mock import Mock

import pytest
from botocore.exceptions import ReadTimeoutError

from ingestion_registry.contract_candidate import VerifiedCandidate
from ingestion_registry.contract_generation import (
    ContractGenerationError,
    ContractGenerator,
)
from ingestion_registry.identity import TableIdentity
from ingestion_registry.references import S3ObjectReference


FUNCTION_ARN = (
    "arn:aws:lambda:eu-west-2:123456789012:"
    "function:schema-generation:7"
)


@pytest.fixture
def generation_setup():
    table = TableIdentity(
        environment="development",
        source="sandbox-postgres",
        database_name="dms_test",
        schema_name="public",
        table_name="dms_integration_test",
    )

    key = "CONTRACTVERSIONREQUEST#" + "a" * 64
    fingerprint = "b" * 64

    reservation = {
        "pk": key,
        "sk": "META",
        "record_type": "CONTRACT_VERSION_RESERVATION",
        "table_identity": asdict(table),
        "request_id": "generation-request-one",
        "request_fingerprint": fingerprint,
        "contract_version": "v2",
    }

    reference = S3ObjectReference(
        bucket_name="example-candidates",
        object_key="candidates/request-one/contract.avsc",
        version_id="candidate-version-one",
        sha256="c" * 64,
    )

    candidate = {
        "source": table.source,
        "database_name": table.database_name,
        "schema_name": table.schema_name,
        "table_name": table.table_name,
        "contract_version": "v2",
        "status": "CANDIDATE_STORED",
        "candidate_reference": asdict(reference),
        "contract_fingerprint": "d" * 64,
    }

    registry = Mock()
    registry._read.return_value = reservation

    reader = Mock()
    reader.read.return_value = VerifiedCandidate(
        reference=reference,
        contract_version="v2",
        contract_fingerprint="d" * 64,
        content=b"verified separately by the reader",
    )

    client = Mock()

    def respond(result=None, **changes):
        payload = io.BytesIO(
            json.dumps(
                result
                if result is not None
                else {
                    "status": "SUCCEEDED",
                    "contracts": [candidate],
                }
            ).encode("utf-8")
        )

        client.invoke.return_value = {
            "StatusCode": 200,
            "ExecutedVersion": "7",
            "Payload": payload,
            **changes,
        }
        return payload

    respond()

    generator = ContractGenerator(
        registry=registry,
        candidate_reader=reader,
        lambda_client=client,
        generation_function_arn=FUNCTION_ARN,
    )

    return {
        "table": table,
        "key": key,
        "fingerprint": fingerprint,
        "reservation": reservation,
        "reference": reference,
        "candidate": candidate,
        "registry": registry,
        "reader": reader,
        "client": client,
        "generator": generator,
        "respond": respond,
    }


def generate(setup):
    return setup["generator"].generate(
        table=setup["table"],
        reservation_key=setup["key"],
        request_fingerprint=setup["fingerprint"],
    )


def test_generation_invokes_reserved_version_and_verifies_candidate(
    generation_setup,
):
    setup = generation_setup

    result = generate(setup)

    invocation = setup["client"].invoke.call_args.kwargs
    request = json.loads(invocation["Payload"])

    assert invocation["FunctionName"] == FUNCTION_ARN
    assert invocation["InvocationType"] == "RequestResponse"
    assert request["operation"] == "GENERATE_CANDIDATE"
    assert request["contract_version"] == "v2"
    assert request["request_id"] == "generation-request-one"
    assert request["version_reservation"] == {
        "request_key": setup["key"],
        "request_fingerprint": setup["fingerprint"],
    }

    setup["reader"].read.assert_called_once_with(
        table=setup["table"],
        contract_version="v2",
        reference=setup["reference"],
    )

    assert result["status"] == "CANDIDATE_VERIFIED"
    assert result["candidate_reference"] == asdict(
        setup["reference"]
    )
    assert result["generation_function_arn"] == FUNCTION_ARN
    assert "content" not in result


@pytest.mark.parametrize(
    "arn",
    [
        FUNCTION_ARN.rsplit(":", 1)[0],
        FUNCTION_ARN.rsplit(":", 1)[0] + ":latest",
        FUNCTION_ARN.rsplit(":", 1)[0] + ":$LATEST",
    ],
)
def test_generation_requires_a_published_numeric_version(
    generation_setup, arn
):
    setup = generation_setup

    with pytest.raises(ValueError, match="numeric"):
        ContractGenerator(
            registry=setup["registry"],
            candidate_reader=setup["reader"],
            lambda_client=setup["client"],
            generation_function_arn=arn,
        )


@pytest.mark.parametrize(
    "change",
    ["missing", "fingerprint", "table"],
)
def test_invalid_reservation_prevents_invocation(
    generation_setup, change
):
    setup = generation_setup

    if change == "missing":
        setup["registry"]._read.return_value = None
    elif change == "fingerprint":
        setup["reservation"]["request_fingerprint"] = "e" * 64
    else:
        setup["reservation"]["table_identity"]["table_name"] = (
            "another_table"
        )

    with pytest.raises(ContractGenerationError, match="reservation"):
        generate(setup)

    setup["client"].invoke.assert_not_called()
    setup["reader"].read.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"StatusCode": 202},
        {"ExecutedVersion": "8"},
        {"FunctionError": "Unhandled"},
    ],
)
def test_failed_invocation_is_not_candidate_completion(
    generation_setup, changes
):
    setup = generation_setup
    payload = setup["respond"](**changes)

    with pytest.raises(ContractGenerationError, match="invocation"):
        generate(setup)

    assert payload.closed
    setup["reader"].read.assert_not_called()


@pytest.mark.parametrize(
    "change",
    ["table", "version", "multiple", "status"],
)
def test_unexpected_handler_result_is_rejected(
    generation_setup, change
):
    setup = generation_setup
    candidate = copy.deepcopy(setup["candidate"])
    result = {"status": "SUCCEEDED", "contracts": [candidate]}

    if change == "table":
        candidate["table_name"] = "another_table"
    elif change == "version":
        candidate["contract_version"] = "v3"
    elif change == "multiple":
        result["contracts"].append(copy.deepcopy(candidate))
    else:
        result["status"] = "FAILED"

    setup["respond"](result)

    with pytest.raises(ContractGenerationError):
        generate(setup)

    setup["reader"].read.assert_not_called()


def test_response_fingerprint_must_match_verified_content(
    generation_setup,
):
    setup = generation_setup
    setup["candidate"]["contract_fingerprint"] = "e" * 64
    setup["respond"]()

    with pytest.raises(ContractGenerationError, match="fingerprint"):
        generate(setup)


def test_candidate_verification_failure_propagates(
    generation_setup,
):
    setup = generation_setup
    setup["reader"].read.side_effect = ValueError(
        "Candidate checksum mismatch"
    )

    with pytest.raises(ValueError, match="checksum"):
        generate(setup)


def test_changed_reservation_blocks_completion(
    generation_setup,
):
    setup = generation_setup
    changed = copy.deepcopy(setup["reservation"])
    changed["request_fingerprint"] = "e" * 64

    setup["registry"]._read.side_effect = [
        setup["reservation"],
        changed,
    ]

    with pytest.raises(ContractGenerationError, match="changed"):
        generate(setup)


def test_invocation_timeout_remains_an_uncertain_outcome(
    generation_setup,
):
    setup = generation_setup
    setup["client"].invoke.side_effect = ReadTimeoutError(
        endpoint_url="https://lambda.eu-west-2.amazonaws.com"
    )

    with pytest.raises(ReadTimeoutError):
        generate(setup)

    setup["reader"].read.assert_not_called()


def test_oversized_response_is_rejected_and_closed(
    generation_setup,
):
    setup = generation_setup
    payload = io.BytesIO(b"x" * 65537)
    setup["client"].invoke.return_value["Payload"] = payload

    with pytest.raises(ContractGenerationError, match="size limit"):
        generate(setup)

    assert payload.closed