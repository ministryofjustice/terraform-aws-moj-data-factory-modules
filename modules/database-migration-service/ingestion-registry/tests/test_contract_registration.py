import copy
import io
import json
from dataclasses import asdict
from unittest.mock import Mock

import pytest

from ingestion_registry.contract_registration import (
    ContractRegistrar,
    ContractRegistrationBlocked,
    ContractRegistrationFailed,
)
from ingestion_registry.contract_verification import (
    ContractNotRegistered,
)
from ingestion_registry.contracts import RegisteredContract
from ingestion_registry.identity import TableIdentity
from ingestion_registry.references import S3ObjectReference


FUNCTION_ARN = (
    "arn:aws:lambda:eu-west-2:123456789012:"
    "function:contract-registration:7"
)


@pytest.fixture
def registration_setup():
    table = TableIdentity(
        environment="development",
        source="sandbox-postgres",
        database_name="dms_test",
        schema_name="public",
        table_name="dms_integration_test",
    )
    contract = RegisteredContract(
        contract_id="example-contract-id",
        contract_version="v1",
        contract_fingerprint="a" * 64,
        object_reference=S3ObjectReference(
            bucket_name="example-contracts",
            object_key=(
                "sandbox-postgres/dms_test/public/"
                "dms_integration_test/v1/contract.avsc"
            ),
            version_id="promoted-one",
            sha256="b" * 64,
        ),
    )
    candidate = {
        "bucket_name": "example-candidates",
        "object_key": "candidates/request-one/contract.avsc",
        "version_id": "candidate-one",
        "sha256": "b" * 64,
    }
    approval = {
        "bucket_name": "example-evidence",
        "object_key": "approvals/review-one.json",
        "version_id": "approval-one",
        "sha256": "c" * 64,
    }
    promoted = {
        "request_key": "CONTRACTREQUEST#example",
        "request_fingerprint": "d" * 64,
        "approval_revision": 2,
        "approval_reference": approval,
        "candidate_reference": candidate,
        "contract_version": "v1",
        "contract_fingerprint": contract.contract_fingerprint,
        "object_reference": asdict(contract.object_reference),
    }

    promoter = Mock()
    promoter.promote.return_value = promoted
    gate = Mock()
    gate.authorise_registration.return_value = {
        "pk": promoted["request_key"],
        "request_fingerprint": promoted["request_fingerprint"],
        "approval_revision": 2,
        "approval_reference": approval,
        "assessment": {"candidate_reference": candidate},
    }
    verifier = Mock()
    verifier.verify.side_effect = [
        ContractNotRegistered("Not registered"),
        contract,
    ]

    payload = io.BytesIO(
        json.dumps(
            {
                "statusCode": 200,
                "results": [{"status": "REGISTERED"}],
            }
        ).encode("utf-8")
    )
    client = Mock()
    client.invoke.return_value = {
        "StatusCode": 200,
        "ExecutedVersion": "7",
        "Payload": payload,
    }

    registrar = ContractRegistrar(
        promoter=promoter,
        approval_gate=gate,
        verifier=verifier,
        lambda_client=client,
        registration_function_arn=FUNCTION_ARN,
    )

    return {
        "table": table,
        "contract": contract,
        "promoter": promoter,
        "gate": gate,
        "verifier": verifier,
        "client": client,
        "payload": payload,
        "registrar": registrar,
    }


def register(setup):
    return setup["registrar"].register(
        table=setup["table"],
        request_id="request-one",
        contract_version="v1",
    )


def test_invokes_registration_and_verifies_durable_result(
    registration_setup,
):
    setup = registration_setup

    result = register(setup)

    assert result["status"] == "REGISTRATION_CONFIRMED"
    assert result["registered_contract"] == asdict(setup["contract"])
    assert setup["verifier"].verify.call_count == 2
    assert setup["gate"].authorise_registration.call_count == 2
    assert setup["payload"].closed

    invocation = setup["client"].invoke.call_args.kwargs
    assert invocation["FunctionName"] == FUNCTION_ARN
    assert invocation["InvocationType"] == "RequestResponse"
    event = json.loads(invocation["Payload"])
    assert event["Records"][0]["s3"]["object"] == {
        "key": (
            "sandbox-postgres%2Fdms_test%2Fpublic%2F"
            "dms_integration_test%2Fv1%2Fcontract.avsc"
        ),
        "versionId": "promoted-one",
    }


def test_existing_registration_is_reconciled_without_invocation(
    registration_setup,
):
    setup = registration_setup
    setup["verifier"].verify.side_effect = None
    setup["verifier"].verify.return_value = setup["contract"]

    result = register(setup)

    assert result["status"] == "REGISTRATION_CONFIRMED"
    setup["client"].invoke.assert_not_called()
    setup["payload"].close()


def test_success_response_without_registration_is_not_success(
    registration_setup,
):
    setup = registration_setup
    setup["verifier"].verify.side_effect = [
        ContractNotRegistered("Missing"),
        ContractNotRegistered("Still missing"),
    ]

    with pytest.raises(ContractNotRegistered):
        register(setup)

    assert setup["payload"].closed


def test_registered_content_must_match_promotion(registration_setup):
    setup = registration_setup
    setup["promoter"].promote.return_value[
        "contract_fingerprint"
    ] = "e" * 64

    with pytest.raises(ContractRegistrationBlocked, match="exact promoted"):
        register(setup)


def test_changed_approval_prevents_invocation(registration_setup):
    setup = registration_setup
    setup["gate"].authorise_registration.return_value[
        "approval_revision"
    ] = 3

    with pytest.raises(
        ContractRegistrationBlocked,
        match="current authorisation",
    ):
        register(setup)

    setup["client"].invoke.assert_not_called()
    setup["payload"].close()


def test_changed_approval_after_registration_blocks_progression(
    registration_setup,
):
    setup = registration_setup
    initial = setup["gate"].authorise_registration.return_value
    changed = copy.deepcopy(initial)
    changed["approval_revision"] = 3
    setup["gate"].authorise_registration.side_effect = [
        initial,
        changed,
    ]

    with pytest.raises(
        ContractRegistrationBlocked,
        match="current authorisation",
    ):
        register(setup)

    assert setup["payload"].closed


@pytest.mark.parametrize(
    "changes",
    [
        {"StatusCode": 202},
        {"FunctionError": "Unhandled"},
        {"ExecutedVersion": "8"},
    ],
)
def test_unsuccessful_invocation_is_rejected(
    registration_setup, changes
):
    setup = registration_setup
    setup["client"].invoke.return_value.update(changes)

    with pytest.raises(ContractRegistrationFailed):
        register(setup)

    assert setup["payload"].closed
    assert setup["verifier"].verify.call_count == 1


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"statusCode": 200, "results": []},
        {
            "statusCode": 200,
            "results": [{"status": "ACCEPTED"}],
        },
    ],
)
def test_incomplete_handler_result_is_rejected(
    registration_setup, result
):
    setup = registration_setup
    setup["payload"].close()
    replacement = io.BytesIO(json.dumps(result).encode("utf-8"))
    setup["client"].invoke.return_value["Payload"] = replacement

    with pytest.raises(ContractRegistrationFailed):
        register(setup)

    assert replacement.closed


@pytest.mark.parametrize(
    "arn",
    [
        FUNCTION_ARN.rsplit(":", 1)[0],
        FUNCTION_ARN.rsplit(":", 1)[0] + ":$LATEST",
        FUNCTION_ARN.rsplit(":", 1)[0] + ":current",
    ],
)
def test_mutable_function_reference_is_rejected(
    registration_setup, arn
):
    setup = registration_setup

    with pytest.raises(ValueError, match="published numeric"):
        ContractRegistrar(
            promoter=setup["promoter"],
            approval_gate=setup["gate"],
            verifier=setup["verifier"],
            lambda_client=setup["client"],
            registration_function_arn=arn,
        )

    setup["payload"].close()