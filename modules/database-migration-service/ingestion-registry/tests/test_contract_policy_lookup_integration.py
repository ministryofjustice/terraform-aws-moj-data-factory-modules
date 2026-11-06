import copy

import pytest

from ingestion_registry.contract_lookup import (
    ContractLookupBlocked,
    CurrentContractLookup,
)
from ingestion_registry.contract_policy import ContractPolicy
from ingestion_registry.contract_policy_authorisation import (
    ContractPolicyAuthorisation,
    ContractPolicyAuthorisationBlocked,
)

from test_contract_policy_activation_integration import (
    policy_activation as policy_activation,
)
from test_contract_policy_registration_completion_integration import (
    policy_completion as policy_completion,
)
from test_contract_policy_authorisation_integration import (
    policy_authorisation_setup as policy_authorisation_setup,
)
from test_contract_reuse_integration import (
    reuse_setup as reuse_setup,
)


@pytest.fixture
def policy_lookup(policy_activation):
    setup = policy_activation

    activated = setup["policy_activation"].activate(
        table=setup["table"],
        request_id="policy-request",
        contract_version="v2",
    )

    lookup = CurrentContractLookup(
        registry=setup["registry"],
        approval_gate=setup["promoter"].approval_gate,
        verifier=setup["reuse"].verifier,
        policy_authorisation=setup["policy_authorisation"],
    )

    return {
        **setup,
        "activated": activated,
        "policy_lookup": lookup,
    }


def resolve(setup):
    return setup["policy_lookup"].resolve(
        table=setup["table"],
    )


def test_lookup_verifies_policy_authorised_current_contract(
    policy_lookup,
):
    setup = policy_lookup
    calls_before = setup["invocation"].calls

    result = resolve(setup)

    assert result["selection"] == setup["activated"]["selection"]
    assert result["authorisation_kind"] == "POLICY"
    assert result["authorisation_revision"] == (
        setup["policy_request"]["revision"]
    )
    assert result["selection"]["registered_contract"][
        "contract_version"
    ] == "v2"

    assert resolve(setup) == result
    assert setup["invocation"].calls == calls_before


def test_lookup_without_policy_configuration_blocks_policy_selection(
    policy_lookup,
):
    setup = policy_lookup

    lookup = CurrentContractLookup(
        registry=setup["registry"],
        approval_gate=setup["promoter"].approval_gate,
        verifier=setup["reuse"].verifier,
    )

    with pytest.raises(ContractLookupBlocked, match="not configured"):
        lookup.resolve(table=setup["table"])


def test_disabled_policy_blocks_current_policy_selection(
    policy_lookup,
):
    setup = policy_lookup

    disabled = ContractPolicyAuthorisation(
        assessment_admission=setup["policy_admission"],
        trusted_policy=ContractPolicy(
            revision="compatible-policy-v1",
        ),
    )

    lookup = CurrentContractLookup(
        registry=setup["registry"],
        approval_gate=setup["promoter"].approval_gate,
        verifier=setup["reuse"].verifier,
        policy_authorisation=disabled,
    )

    with pytest.raises(
        ContractPolicyAuthorisationBlocked,
        match="disabled",
    ):
        lookup.resolve(table=setup["table"])


def test_changed_authorisation_receipt_blocks_lookup(
    policy_lookup,
):
    setup = policy_lookup
    key = setup["policy_request"]["pk"]

    receipt = setup["registry"]._read(
        key,
        "REGISTRATION_AUTHORIZATION",
    )
    receipt["authorisation_revision"] += 1

    setup["registry"].client.put_item(
        TableName=setup["registry"].table_name,
        Item=setup["registry"]._encode(receipt),
    )

    with pytest.raises(
        ContractLookupBlocked,
        match="registration authorisation",
    ):
        resolve(setup)


def test_missing_activation_evidence_blocks_lookup(
    policy_lookup,
):
    setup = policy_lookup

    setup["registry"].client.delete_item(
        TableName=setup["registry"].table_name,
        Key=setup["registry"]._encode(
            {
                "pk": setup["policy_request"]["pk"],
                "sk": "ACTIVATION",
            }
        ),
    )

    with pytest.raises(
        ContractLookupBlocked,
        match="registration and activation",
    ):
        resolve(setup)


def test_selection_change_during_verification_blocks_lookup(
    policy_lookup,
    monkeypatch,
):
    setup = policy_lookup
    verifier = setup["reuse"].verifier
    original_verify = verifier.verify

    def verify_then_change_selection(**kwargs):
        contract = original_verify(**kwargs)

        changed = copy.deepcopy(
            setup["activated"]["selection"]
        )
        changed["revision"] += 1

        setup["registry"].client.put_item(
            TableName=setup["registry"].table_name,
            Item=setup["registry"]._encode(changed),
        )

        return contract

    monkeypatch.setattr(
        verifier,
        "verify",
        verify_then_change_selection,
    )

    with pytest.raises(
        ContractLookupBlocked,
        match="changed during lookup",
    ):
        resolve(setup)