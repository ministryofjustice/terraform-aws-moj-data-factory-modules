import pytest
from botocore.exceptions import ReadTimeoutError

from ingestion_registry.contract_registration import (
    ContractRegistrationBlocked,
)
from ingestion_registry.contract_registration_completion import (
    ContractRegistrationCompletion,
)

from test_contract_policy_authorisation_integration import (
    policy_authorisation_setup as policy_authorisation_setup,
)
from test_contract_policy_registration_integration import registrar
from test_contract_reuse_integration import (
    reuse_setup as reuse_setup,
)


@pytest.fixture
def policy_completion(policy_authorisation_setup):
    setup = policy_authorisation_setup

    completion = ContractRegistrationCompletion(
        registrar=registrar(setup),
        approval_gate=setup["promoter"].approval_gate,
        policy_authorisation=setup["policy_authorisation"],
    )

    return {**setup, "policy_completion": completion}


def complete(setup):
    return setup["policy_completion"].complete(
        table=setup["table"],
        request_id="policy-request",
        contract_version="v2",
    )


def writes_registration(kwargs):
    return any(
        action.get("Put", {})
        .get("Item", {})
        .get("sk", {})
        .get("S") == "REGISTRATION"
        for action in kwargs["TransactItems"]
    )


def test_policy_registration_completion_is_recorded(
    policy_completion,
):
    setup = policy_completion

    record = complete(setup)

    assert setup["registry"]._read(
        setup["policy_request"]["pk"],
        "REGISTRATION",
    ) == record

    result = record["result"]
    assert result["status"] == "REGISTRATION_CONFIRMED"
    assert result["authorisation"]["kind"] == "POLICY"
    assert result["registered_contract"]["contract_version"] == "v2"
    assert "approval_reference" not in result
    assert "approval_revision" not in result

    # Completion does not activate the new contract.
    assert setup["registry"]._read(
        setup["table"].partition_key,
        "CONTRACT_CURRENT",
    ) == setup["selection"]


def test_duplicate_policy_completion_returns_original_record(
    policy_completion,
):
    setup = policy_completion

    original = complete(setup)
    calls_after_first = setup["invocation"].calls

    assert complete(setup) == original
    assert setup["invocation"].calls == calls_after_first


def test_lost_completion_response_reconciles_original_record(
    policy_completion,
    monkeypatch,
):
    setup = policy_completion
    client = setup["registry"].client
    original_write = client.transact_write_items

    def write_then_lose_completion_response(**kwargs):
        response = original_write(**kwargs)

        if writes_registration(kwargs):
            raise ReadTimeoutError(
                endpoint_url="https://dynamodb.eu-west-2.amazonaws.com"
            )

        return response

    monkeypatch.setattr(
        client,
        "transact_write_items",
        write_then_lose_completion_response,
    )

    with pytest.raises(ReadTimeoutError):
        complete(setup)

    stored = setup["registry"]._read(
        setup["policy_request"]["pk"],
        "REGISTRATION",
    )

    assert stored is not None

    monkeypatch.setattr(
        client,
        "transact_write_items",
        original_write,
    )

    assert complete(setup) == stored


def test_policy_change_before_completion_blocks_progression(
    policy_completion,
    monkeypatch,
):
    setup = policy_completion
    client = setup["registry"].client
    original_write = client.transact_write_items

    def change_decision_before_completion(**kwargs):
        if writes_registration(kwargs):
            request = setup["lifecycle"].read(
                table=setup["table"],
                request_id="policy-request",
            )
            request["status"] = "BLOCKED"

            client.put_item(
                TableName=setup["registry"].table_name,
                Item=setup["registry"]._encode(request),
            )

        return original_write(**kwargs)

    monkeypatch.setattr(
        client,
        "transact_write_items",
        change_decision_before_completion,
    )

    with pytest.raises(
        ContractRegistrationBlocked,
        match="Policy decision or authorisation changed",
    ):
        complete(setup)

    assert setup["registry"]._read(
        setup["policy_request"]["pk"],
        "REGISTRATION",
    ) is None

    # Registration may already exist, but progression is blocked.
    assert setup["registration_table"].scan(
        ConsistentRead=True
    )["Count"] == 2


def test_changed_authorisation_receipt_blocks_completion(
    policy_completion,
    monkeypatch,
):
    setup = policy_completion
    client = setup["registry"].client
    original_write = client.transact_write_items

    def change_receipt_before_completion(**kwargs):
        if writes_registration(kwargs):
            receipt = setup["registry"]._read(
                setup["policy_request"]["pk"],
                "REGISTRATION_AUTHORIZATION",
            )
            receipt["authorisation_revision"] += 1

            client.put_item(
                TableName=setup["registry"].table_name,
                Item=setup["registry"]._encode(receipt),
            )

        return original_write(**kwargs)

    monkeypatch.setattr(
        client,
        "transact_write_items",
        change_receipt_before_completion,
    )

    with pytest.raises(
        ContractRegistrationBlocked,
        match="Policy decision or authorisation changed",
    ):
        complete(setup)

    assert setup["registry"]._read(
        setup["policy_request"]["pk"],
        "REGISTRATION",
    ) is None