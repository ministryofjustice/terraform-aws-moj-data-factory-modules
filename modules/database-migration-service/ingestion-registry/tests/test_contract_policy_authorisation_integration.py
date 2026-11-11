import copy

import pytest
from botocore.exceptions import ClientError

from contract_integration_support import encode, store_reference
from ingestion_registry.contract_assessment import (
    ContractAssessmentAdmission,
)
from ingestion_registry.contract_authorisation import (
    policy_acceptance_check,
)
from ingestion_registry.contract_policy import ContractPolicy
from ingestion_registry.contract_policy_authorisation import (
    ContractPolicyAuthorisation,
    ContractPolicyAuthorisationBlocked,
)

from test_contract_reuse_integration import (
    reuse_setup as reuse_setup,
)


@pytest.fixture
def policy_authorisation_setup(reuse_setup):
    setup = reuse_setup
    policy = ContractPolicy(
        revision="compatible-policy-v1",
        automatically_accept_compatible=True,
    )

    contract = copy.deepcopy(setup["contract"])
    contract["contract_version"] = "v2"
    contract["description"] = "Candidate assessed as compatible"

    candidate = store_reference(
        setup["s3"],
        "test-contract-candidates",
        "candidates/policy-request/contract.avsc",
        encode(contract),
    )

    document = {
        "validation_result_version": 1,
        "request_id": "policy-request",
        "identity": {
            field: contract[field]
            for field in (
                "source",
                "database_name",
                "schema_name",
                "table_name",
                "contract_version",
            )
        },
        "candidate_reference": {
            "bucket_name": candidate.bucket_name,
            "object_key": candidate.object_key,
            "version_id": candidate.version_id,
            "sha256": candidate.sha256,
        },
        "baseline_reference": setup["selection"][
            "registered_contract"
        ]["object_reference"],
        "assessor_revision": "test-assessor-v1",
        "validation": "VALID",
        "compatibility": "COMPATIBLE",
    }

    evidence = store_reference(
        setup["s3"],
        "test-contract-evidence",
        "validation/policy-request/result.json",
        encode(document),
    )

    admission = ContractAssessmentAdmission(
        lifecycle=setup["lifecycle"],
        candidate_reader=setup["promoter"].candidate_reader,
        current_lookup=setup["lookup"],
        s3_client=setup["s3"],
        evidence_bucket="test-contract-evidence",
        evidence_prefix="validation/",
        bucket_owner_account_id="123456789012",
        assessor_revision="test-assessor-v1",
    )

    request = admission.submit(
        table=setup["table"],
        request_id="policy-request",
        contract_version="v2",
        candidate_reference=candidate,
        evidence_reference=evidence,
        policy=policy,
    )

    service = ContractPolicyAuthorisation(
        assessment_admission=admission,
        trusted_policy=policy,
    )

    return {
        **setup,
        "policy": policy,
        "policy_request": request,
        "policy_admission": admission,
        "policy_authorisation": service,
        "policy_evidence": evidence,
    }


def verify(setup, service=None):
    return (
        setup["policy_authorisation"]
        if service is None
        else service
    ).verify(
        table=setup["table"],
        request_id="policy-request",
        contract_version="v2",
    )


def test_compatible_assessment_has_explicit_policy_authorisation(
    policy_authorisation_setup,
):
    setup = policy_authorisation_setup
    calls_before = setup["invocation"].calls

    result = verify(setup)

    assert result["authorisation_kind"] == "POLICY"
    assert result["policy"]["automatically_accept_compatible"] is True
    assert result["assessment"]["compatibility"] == "COMPATIBLE"

    request = setup["lifecycle"].read(
        table=setup["table"],
        request_id="policy-request",
    )
    assert request["status"] == "POLICY_ACCEPTED"
    assert "review" not in request

    assert setup["invocation"].calls == calls_before
    assert setup["registration_table"].scan(
        ConsistentRead=True
    )["Count"] == 1


def test_disabled_trusted_policy_blocks_acceptance(
    policy_authorisation_setup,
):
    setup = policy_authorisation_setup

    disabled = ContractPolicyAuthorisation(
        assessment_admission=setup["policy_admission"],
        trusted_policy=ContractPolicy(
            revision="compatible-policy-v1",
        ),
    )

    with pytest.raises(
        ContractPolicyAuthorisationBlocked,
        match="disabled",
    ):
        verify(setup, disabled)


def test_different_trusted_policy_revision_is_rejected(
    policy_authorisation_setup,
):
    setup = policy_authorisation_setup

    changed = ContractPolicyAuthorisation(
        assessment_admission=setup["policy_admission"],
        trusted_policy=ContractPolicy(
            revision="compatible-policy-v2",
            automatically_accept_compatible=True,
        ),
    )

    with pytest.raises(
        ContractPolicyAuthorisationBlocked,
        match="trusted policy",
    ):
        verify(setup, changed)


def test_human_approved_request_is_not_policy_authorisation(
    policy_authorisation_setup,
):
    setup = policy_authorisation_setup

    with pytest.raises(
        ContractPolicyAuthorisationBlocked,
        match="not accepted under policy",
    ):
        setup["policy_authorisation"].verify(
            table=setup["table"],
            request_id="request-one",
            contract_version="v1",
        )


def test_policy_verification_is_repeatable(
    policy_authorisation_setup,
):
    setup = policy_authorisation_setup

    assert verify(setup) == verify(setup)


def test_policy_guard_detects_concurrent_decision_change(
    policy_authorisation_setup,
):
    setup = policy_authorisation_setup
    verified = verify(setup)

    guard = policy_acceptance_check(
        registry=setup["registry"],
        verified=verified,
    )

    setup["registry"].client.transact_write_items(
        TransactItems=[guard],
    )

    request = setup["lifecycle"].read(
        table=setup["table"],
        request_id="policy-request",
    )
    request["status"] = "BLOCKED"

    setup["registry"].client.put_item(
        TableName=setup["registry"].table_name,
        Item=setup["registry"]._encode(request),
    )

    with pytest.raises(ClientError) as raised:
        setup["registry"].client.transact_write_items(
            TransactItems=[guard],
        )

    assert raised.value.response["Error"]["Code"] == (
        "TransactionCanceledException"
    )