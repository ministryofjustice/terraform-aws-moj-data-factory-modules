import pytest
from botocore.exceptions import ReadTimeoutError

from ingestion_registry.contract_policy_authorisation import (
    ContractPolicyAuthorisationBlocked,
)

from test_contract_policy_authorisation_integration import (
    policy_authorisation_setup as policy_authorisation_setup,
)
from test_contract_reuse_integration import (
    reuse_setup as reuse_setup,
)


def authorise(setup):
    return setup["policy_authorisation"].authorise_registration(
        table=setup["table"],
        request_id="policy-request",
        contract_version="v2",
    )


def test_policy_authorisation_is_recorded_explicitly(
    policy_authorisation_setup,
):
    setup = policy_authorisation_setup
    calls_before = setup["invocation"].calls

    result = authorise(setup)

    stored = setup["registry"]._read(
        setup["policy_request"]["pk"],
        "REGISTRATION_AUTHORIZATION",
    )

    assert stored == result
    assert result["authorisation_kind"] == "POLICY"
    assert result["contract_version"] == "v2"
    assert result["authorisation_evidence_reference"] == (
        setup["policy_request"]["assessment"]["evidence_reference"]
    )
    assert result["policy"]["automatically_accept_compatible"] is True

    assert "approval_reference" not in result
    assert "approval_revision" not in result

    assert setup["invocation"].calls == calls_before
    assert setup["registration_table"].scan(
        ConsistentRead=True
    )["Count"] == 1

    assert setup["registry"]._read(
        setup["table"].partition_key,
        "CONTRACT_CURRENT",
    ) == setup["selection"]


def test_duplicate_policy_authorisation_returns_original_record(
    policy_authorisation_setup,
):
    setup = policy_authorisation_setup

    original = authorise(setup)

    assert authorise(setup) == original


def test_lost_authorisation_response_reconciles_original_record(
    policy_authorisation_setup,
    monkeypatch,
):
    setup = policy_authorisation_setup
    client = setup["registry"].client
    original_write = client.transact_write_items

    def write_then_lose_response(**kwargs):
        original_write(**kwargs)
        raise ReadTimeoutError(
            endpoint_url="https://dynamodb.eu-west-2.amazonaws.com"
        )

    monkeypatch.setattr(
        client,
        "transact_write_items",
        write_then_lose_response,
    )

    with pytest.raises(ReadTimeoutError):
        authorise(setup)

    stored = setup["registry"]._read(
        setup["policy_request"]["pk"],
        "REGISTRATION_AUTHORIZATION",
    )

    assert stored is not None

    monkeypatch.setattr(
        client,
        "transact_write_items",
        original_write,
    )

    assert authorise(setup) == stored


def test_conflicting_authorisation_cannot_be_overwritten(
    policy_authorisation_setup,
):
    setup = policy_authorisation_setup

    conflicting = {
        "pk": setup["policy_request"]["pk"],
        "sk": "REGISTRATION_AUTHORIZATION",
        "record_type": "CONTRACT_REGISTRATION_AUTHORIZATION",
        "authorisation_kind": "HUMAN",
        "request_fingerprint": "f" * 64,
    }

    setup["registry"].client.put_item(
        TableName=setup["registry"].table_name,
        Item=setup["registry"]._encode(conflicting),
    )

    with pytest.raises(
        ContractPolicyAuthorisationBlocked,
        match="different evidence or authorisation type",
    ):
        authorise(setup)

    assert setup["registry"]._read(
        conflicting["pk"],
        conflicting["sk"],
    ) == conflicting


def test_decision_change_before_write_blocks_authorisation(
    policy_authorisation_setup,
    monkeypatch,
):
    setup = policy_authorisation_setup
    service = setup["policy_authorisation"]
    original_verify = service.verify

    def verify_then_change_decision(**kwargs):
        verified = original_verify(**kwargs)

        request = setup["lifecycle"].read(
            table=setup["table"],
            request_id="policy-request",
        )
        request["status"] = "BLOCKED"

        setup["registry"].client.put_item(
            TableName=setup["registry"].table_name,
            Item=setup["registry"]._encode(request),
        )

        return verified

    monkeypatch.setattr(
        service,
        "verify",
        verify_then_change_decision,
    )

    with pytest.raises(
        ContractPolicyAuthorisationBlocked,
        match="decision changed",
    ):
        authorise(setup)

    assert setup["registry"]._read(
        setup["policy_request"]["pk"],
        "REGISTRATION_AUTHORIZATION",
    ) is None