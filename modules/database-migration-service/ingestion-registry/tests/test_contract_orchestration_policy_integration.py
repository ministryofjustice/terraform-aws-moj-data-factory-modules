import pytest

from ingestion_registry.contract_activation import ContractActivation
from ingestion_registry.contract_lookup import CurrentContractLookup
from ingestion_registry.contract_orchestration import (
    ContractOrchestration,
    ContractOrchestrationBlocked,
)
from ingestion_registry.contract_policy import ContractPolicy
from ingestion_registry.contract_reuse import ContractReuse

from test_contract_policy_registration_completion_integration import (
    policy_completion as policy_completion,
)
from test_contract_policy_authorisation_integration import (
    policy_authorisation_setup as policy_authorisation_setup,
)
from test_contract_reuse_integration import (
    reuse_setup as reuse_setup,
)


def coordinator(setup, trusted_policy=None):
    lookup = CurrentContractLookup(
        registry=setup["registry"],
        approval_gate=setup["promoter"].approval_gate,
        verifier=setup["reuse"].verifier,
        policy_authorisation=setup["policy_authorisation"],
    )

    reuse = ContractReuse(
        lifecycle=setup["lifecycle"],
        current_lookup=lookup,
        contract_verifier=setup["reuse"].verifier,
    )

    activation = ContractActivation(
        completion=setup["policy_completion"],
        lifecycle=setup["lifecycle"],
    )

    return ContractOrchestration(
        lifecycle=setup["lifecycle"],
        lookup=lookup,
        assessment_admission=setup["policy_admission"],
        reuse=reuse,
        activation=activation,
        trusted_policy=(
            setup["policy"]
            if trusted_policy is None
            else trusted_policy
        ),
    )


def advance(service, setup):
    return service.advance(
        table=setup["table"],
        request_id="policy-request",
        contract_version="v2",
        expected_request_fingerprint=setup["policy_request"][
            "request_fingerprint"
        ],
    )


def test_enabled_policy_progresses_through_registration_and_activation(
    policy_completion,
):
    setup = policy_completion
    service = coordinator(setup)

    result = advance(service, setup)

    assert result["status"] == "CONTRACT_READY"
    assert result["action"] == "ACTIVATED"
    assert result["selection"]["authorisation"]["kind"] == "POLICY"
    assert result["selection"]["registered_contract"][
        "contract_version"
    ] == "v2"

    calls_after_first = setup["invocation"].calls

    assert advance(service, setup) == result
    assert setup["invocation"].calls == calls_after_first

    resolved = service.resolve(table=setup["table"])

    assert resolved["status"] == "CONTRACT_READY"
    assert resolved["selection"] == result["selection"]


def test_disabled_coordinator_policy_cannot_progress_accepted_request(
    policy_completion,
):
    setup = policy_completion
    calls_before = setup["invocation"].calls

    service = coordinator(
        setup,
        trusted_policy=ContractPolicy(
            revision="compatible-policy-v1",
        ),
    )

    with pytest.raises(
        ContractOrchestrationBlocked,
        match="trusted policy",
    ):
        advance(service, setup)

    assert setup["invocation"].calls == calls_before
    assert setup["registry"]._read(
        setup["table"].partition_key,
        "CONTRACT_CURRENT",
    ) == setup["selection"]