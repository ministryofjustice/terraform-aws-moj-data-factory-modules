from dataclasses import replace

import pytest

from ingestion_registry.contracts import PolicyContractAuthorisation
from ingestion_registry.progress import ProgressConflict

from test_contracts import contract_setup as contract_setup


def policy_evidence(approval):
    return PolicyContractAuthorisation(
        evidence_reference=replace(
            approval,
            object_key="validation/compatible-change/result.json",
        ),
        policy_revision="compatible-policy-v1",
        request_key="CONTRACTREQUEST#" + "b" * 64,
        request_fingerprint="c" * 64,
        request_revision=1,
    )


def test_policy_authorisation_is_pinned_without_human_approval_fields(
    contract_setup,
):
    _, admitted, _, claim, progress, contracts, contract, approval = (
        contract_setup
    )
    authorisation = policy_evidence(approval)

    contracts.pin(
        claim,
        contract=contract,
        policy_authorisation=authorisation,
        update_id="policy-contract-selected",
        expected_revision=0,
    )

    recorded = progress.read_stage(
        ingestion_id=admitted.ingestion_id,
        stage="CONTRACT",
    )["result_reference"]

    assert recorded["contract_id"] == contract.contract_id
    assert recorded["contract_version"] == contract.contract_version
    assert recorded["authorisation_kind"] == "POLICY"
    assert recorded["policy_revision"] == "compatible-policy-v1"
    assert recorded["authorisation_object_version_id"] == (
        authorisation.evidence_reference.version_id
    )
    assert recorded["authorisation_request_fingerprint"] == "c" * 64
    assert recorded["authorisation_request_revision"] == "1"
    assert not any(
        field.startswith("approval_")
        for field in recorded
    )


def test_completed_policy_pin_cannot_be_replaced(
    contract_setup,
):
    _, _, _, claim, _, contracts, contract, approval = contract_setup
    authorisation = policy_evidence(approval)

    contracts.pin(
        claim,
        contract=contract,
        policy_authorisation=authorisation,
        update_id="policy-contract-selected",
        expected_revision=0,
    )

    with pytest.raises(ProgressConflict, match="completed stage"):
        contracts.pin(
            claim,
            contract=contract,
            policy_authorisation=replace(
                authorisation,
                policy_revision="compatible-policy-v2",
            ),
            update_id="replace-policy-selection",
            expected_revision=1,
        )


def test_recovery_retains_original_policy_pin(
    contract_setup,
):
    ownership, admitted, clock, claim, _, contracts, contract, approval = (
        contract_setup
    )

    arguments = {
        "contract": contract,
        "policy_authorisation": policy_evidence(approval),
        "update_id": "policy-contract-selected",
        "expected_revision": 0,
    }

    original = contracts.pin(claim, **arguments)
    clock["now"] = claim.lease_expires_at

    recovered = ownership.claim(
        ingestion_id=admitted.ingestion_id,
        execution_id="execution-two",
    )

    assert contracts.pin(recovered, **arguments) == original


def test_human_and_policy_authorisation_cannot_be_combined(
    contract_setup,
):
    _, _, _, claim, _, contracts, contract, approval = contract_setup

    with pytest.raises(ValueError, match="not both"):
        contracts.pin(
            claim,
            contract=contract,
            approval_reference=approval,
            policy_authorisation=policy_evidence(approval),
            update_id="mixed-authorisation",
            expected_revision=0,
        )


def test_policy_evidence_change_cannot_replace_completed_pin(
    contract_setup,
):
    _, _, _, claim, _, contracts, contract, approval = contract_setup
    authorisation = policy_evidence(approval)

    contracts.pin(
        claim,
        contract=contract,
        policy_authorisation=authorisation,
        update_id="policy-contract-selected",
        expected_revision=0,
    )

    changed = replace(
        authorisation,
        evidence_reference=replace(
            authorisation.evidence_reference,
            version_id="different-evidence-version",
        ),
    )

    with pytest.raises(ProgressConflict, match="completed stage"):
        contracts.pin(
            claim,
            contract=contract,
            policy_authorisation=changed,
            update_id="replace-policy-evidence",
            expected_revision=1,
        )