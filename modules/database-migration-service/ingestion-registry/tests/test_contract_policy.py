from dataclasses import replace

import pytest

from ingestion_registry.contract_policy import (
    CompatibilityOutcome,
    ContractAction,
    ContractAssessment,
    ContractPolicy,
    ValidationOutcome,
    decide_contract,
)
from ingestion_registry.references import S3ObjectReference


def reference(key, version):
    return S3ObjectReference(
        bucket_name="example-contract-evidence",
        object_key=key,
        version_id=version,
        sha256="a" * 64,
    )


def assessment(
    compatibility=CompatibilityOutcome.COMPATIBLE,
    validation=ValidationOutcome.VALID,
):
    baseline = (
        None
        if compatibility == CompatibilityOutcome.FIRST_CONTRACT
        else reference("contracts/baseline.avsc", "baseline-one")
    )

    return ContractAssessment(
        validation=validation,
        compatibility=compatibility,
        candidate_reference=reference(
            "candidates/request-one/contract.avsc",
            "candidate-one",
        ),
        baseline_reference=baseline,
        evidence_reference=reference(
            "validation/request-one/result.json",
            "validation-one",
        ),
    )


@pytest.mark.parametrize(
    "compatibility,expected",
    [
        (
            CompatibilityOutcome.FIRST_CONTRACT,
            ContractAction.REQUIRE_APPROVAL,
        ),
        (
            CompatibilityOutcome.NO_CHANGE,
            ContractAction.REUSE_EXISTING,
        ),
        (
            CompatibilityOutcome.COMPATIBLE,
            ContractAction.REQUIRE_APPROVAL,
        ),
        (
            CompatibilityOutcome.BREAKING,
            ContractAction.REQUIRE_APPROVAL,
        ),
        (
            CompatibilityOutcome.UNSUPPORTED,
            ContractAction.BLOCK,
        ),
    ],
)
def test_default_policy_requires_review_or_blocks(
    compatibility, expected
):
    result = decide_contract(
        assessment=assessment(compatibility),
        policy=ContractPolicy(revision="policy-v1"),
    )

    assert result.action == expected
    assert result.policy_revision == "policy-v1"


def test_compatible_change_can_be_accepted_by_explicit_policy():
    result = decide_contract(
        assessment=assessment(),
        policy=ContractPolicy(
            revision="policy-v2",
            automatically_accept_compatible=True,
        ),
    )

    assert result.action == ContractAction.ACCEPT_UNDER_POLICY
    assert result.policy_revision == "policy-v2"


@pytest.mark.parametrize(
    "compatibility",
    [
        CompatibilityOutcome.FIRST_CONTRACT,
        CompatibilityOutcome.BREAKING,
    ],
)
def test_compatible_policy_does_not_bypass_required_approval(
    compatibility,
):
    result = decide_contract(
        assessment=assessment(compatibility),
        policy=ContractPolicy(
            revision="policy-v2",
            automatically_accept_compatible=True,
        ),
    )

    assert result.action == ContractAction.REQUIRE_APPROVAL


@pytest.mark.parametrize(
    "validation",
    [
        ValidationOutcome.INVALID,
        ValidationOutcome.UNSUPPORTED,
    ],
)
@pytest.mark.parametrize("automatic", [False, True])
def test_unsuccessful_validation_always_blocks(
    validation, automatic
):
    result = decide_contract(
        assessment=assessment(
            CompatibilityOutcome.UNSUPPORTED,
            validation=validation,
        ),
        policy=ContractPolicy(
            revision="policy-v1",
            automatically_accept_compatible=automatic,
        ),
    )

    assert result.action == ContractAction.BLOCK


def test_no_change_reuses_baseline_under_automatic_policy():
    result = decide_contract(
        assessment=assessment(CompatibilityOutcome.NO_CHANGE),
        policy=ContractPolicy(
            revision="policy-v2",
            automatically_accept_compatible=True,
        ),
    )

    assert result.action == ContractAction.REUSE_EXISTING


@pytest.mark.parametrize(
    "compatibility",
    [
        CompatibilityOutcome.NO_CHANGE,
        CompatibilityOutcome.COMPATIBLE,
        CompatibilityOutcome.BREAKING,
    ],
)
def test_comparison_requires_exact_baseline(compatibility):
    with pytest.raises(ValueError, match="baseline"):
        replace(
            assessment(compatibility),
            baseline_reference=None,
        )


def test_first_contract_rejects_existing_baseline():
    with pytest.raises(ValueError, match="must not have"):
        replace(
            assessment(CompatibilityOutcome.FIRST_CONTRACT),
            baseline_reference=reference(
                "contracts/baseline.avsc",
                "baseline-one",
            ),
        )


def test_invalid_contract_cannot_claim_compatible():
    with pytest.raises(ValueError, match="unsuccessful validation"):
        replace(
            assessment(),
            validation=ValidationOutcome.INVALID,
        )


@pytest.mark.parametrize(
    "field",
    [
        "candidate_reference",
        "baseline_reference",
        "evidence_reference",
    ],
)
def test_references_must_use_existing_reference_type(field):
    with pytest.raises(ValueError, match=field):
        replace(assessment(), **{field: {"version_id": "untrusted"}})


@pytest.mark.parametrize("revision", ["", " policy-v1", None])
def test_policy_requires_explicit_revision(revision):
    with pytest.raises(ValueError, match="revision"):
        ContractPolicy(revision=revision)


def test_policy_rejects_truthy_string():
    with pytest.raises(ValueError, match="boolean"):
        ContractPolicy(
            revision="policy-v1",
            automatically_accept_compatible="false",
        )


def test_assessment_rejects_unrecognised_outcome():
    with pytest.raises(ValueError, match="compatibility"):
        replace(assessment(), compatibility="PROBABLY_COMPATIBLE")