import pytest

from ingestion_registry.contract_policy import ContractPolicy
from ingestion_registry.contract_policy_authorisation import (
    ContractPolicyAuthorisation,
    ContractPolicyAuthorisationBlocked,
)
from ingestion_registry.contract_promotion import ContractPromoter
from ingestion_registry.contract_registration import ContractRegistrar

from test_contract_policy_authorisation_integration import (
    policy_authorisation_setup as policy_authorisation_setup,
)
from test_contract_reuse_integration import (
    reuse_setup as reuse_setup,
)


def registrar(setup, policy_authorisation=None):
    policy_authorisation = (
        setup["policy_authorisation"]
        if policy_authorisation is None
        else policy_authorisation
    )

    gate = setup["promoter"].approval_gate

    promoter = ContractPromoter(
        approval_gate=gate,
        policy_authorisation=policy_authorisation,
        candidate_reader=setup["promoter"].candidate_reader,
        s3_client=setup["s3"],
        destination_bucket="test-approved-contracts",
        bucket_owner_account_id="123456789012",
        kms_key_arn=setup["kms_key"],
    )

    return ContractRegistrar(
        promoter=promoter,
        approval_gate=gate,
        policy_authorisation=policy_authorisation,
        verifier=setup["reuse"].verifier,
        lambda_client=setup["invocation"],
        registration_function_arn=(
            "arn:aws:lambda:eu-west-2:123456789012:"
            "function:contract-registration:7"
        ),
    )


def register(service, setup):
    return service.register(
        table=setup["table"],
        request_id="policy-request",
        contract_version="v2",
    )


def test_policy_authorised_candidate_is_registered(
    policy_authorisation_setup,
):
    setup = policy_authorisation_setup
    calls_before = setup["invocation"].calls

    result = register(registrar(setup), setup)

    assert result["status"] == "REGISTRATION_CONFIRMED"
    assert result["registered_contract"]["contract_version"] == "v2"
    assert result["authorisation"]["kind"] == "POLICY"
    assert result["authorisation"]["policy"][
        "automatically_accept_compatible"
    ] is True

    assert "approval_reference" not in result
    assert "approval_revision" not in result

    assert setup["invocation"].calls == calls_before + 1
    assert setup["registration_table"].scan(
        ConsistentRead=True
    )["Count"] == 2

    assert setup["registry"]._read(
        setup["table"].partition_key,
        "CONTRACT_CURRENT",
    ) == setup["selection"]

    request = setup["lifecycle"].read(
        table=setup["table"],
        request_id="policy-request",
    )
    assert request["status"] == "POLICY_ACCEPTED"
    assert "review" not in request


def test_duplicate_policy_registration_reuses_existing_registration(
    policy_authorisation_setup,
):
    setup = policy_authorisation_setup
    service = registrar(setup)

    original = register(service, setup)
    calls_after_first = setup["invocation"].calls

    repeated = register(service, setup)

    assert repeated == original
    assert setup["invocation"].calls == calls_after_first


def test_disabled_policy_prevents_promotion_and_registration(
    policy_authorisation_setup,
):
    setup = policy_authorisation_setup

    disabled = ContractPolicyAuthorisation(
        assessment_admission=setup["policy_admission"],
        trusted_policy=ContractPolicy(
            revision="compatible-policy-v1",
        ),
    )

    service = registrar(setup, disabled)
    calls_before = setup["invocation"].calls

    with pytest.raises(
        ContractPolicyAuthorisationBlocked,
        match="disabled",
    ):
        register(service, setup)

    assert setup["invocation"].calls == calls_before
    assert setup["registration_table"].scan(
        ConsistentRead=True
    )["Count"] == 1

    key = (
        "sandbox-postgres/dms_test/public/"
        "dms_integration_test/v2/contract.avsc"
    )

    response = setup["s3"].list_objects_v2(
        Bucket="test-approved-contracts",
        Prefix=key,
    )
    assert response.get("KeyCount", 0) == 0