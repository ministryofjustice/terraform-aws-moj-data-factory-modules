from dataclasses import dataclass
from enum import Enum

from ingestion_registry.references import S3ObjectReference


class ValidationOutcome(str, Enum):
    VALID = "VALID"
    INVALID = "INVALID"
    UNSUPPORTED = "UNSUPPORTED"


class CompatibilityOutcome(str, Enum):
    FIRST_CONTRACT = "FIRST_CONTRACT"
    NO_CHANGE = "NO_CHANGE"
    COMPATIBLE = "COMPATIBLE"
    BREAKING = "BREAKING"
    UNSUPPORTED = "UNSUPPORTED"


class ContractAction(str, Enum):
    BLOCK = "BLOCK"
    REUSE_EXISTING = "REUSE_EXISTING"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    ACCEPT_UNDER_POLICY = "ACCEPT_UNDER_POLICY"


@dataclass(frozen=True)
class ContractAssessment:
    validation: ValidationOutcome
    compatibility: CompatibilityOutcome

    candidate_reference: S3ObjectReference

    baseline_reference: S3ObjectReference | None

    evidence_reference: S3ObjectReference

    def __post_init__(self) -> None:
        if not isinstance(self.validation, ValidationOutcome):
            raise ValueError("validation must be a ValidationOutcome")

        if not isinstance(
            self.compatibility, CompatibilityOutcome
        ):
            raise ValueError(
                "compatibility must be a CompatibilityOutcome"
            )

        for field in (
            "candidate_reference",
            "evidence_reference",
        ):
            if not isinstance(
                getattr(self, field), S3ObjectReference
            ):
                raise ValueError(
                    f"{field} must be an S3ObjectReference"
                )

        if (
            self.baseline_reference is not None
            and not isinstance(
                self.baseline_reference, S3ObjectReference
            )
        ):
            raise ValueError(
                "baseline_reference must be an "
                "S3ObjectReference or None"
            )

        if self.validation != ValidationOutcome.VALID:
            if (
                self.compatibility
                != CompatibilityOutcome.UNSUPPORTED
            ):
                raise ValueError(
                    "An unsuccessful validation cannot assert "
                    "a compatibility outcome"
                )
            return

        if (
            self.compatibility
            == CompatibilityOutcome.FIRST_CONTRACT
        ):
            if self.baseline_reference is not None:
                raise ValueError(
                    "FIRST_CONTRACT must not have a baseline"
                )

        elif self.compatibility in {
            CompatibilityOutcome.NO_CHANGE,
            CompatibilityOutcome.COMPATIBLE,
            CompatibilityOutcome.BREAKING,
        }:
            if self.baseline_reference is None:
                raise ValueError(
                    "Comparison requires an exact baseline reference"
                )


@dataclass(frozen=True)
class ContractPolicy:
    revision: str
    automatically_accept_compatible: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.revision, str)
            or not self.revision
            or self.revision != self.revision.strip()
            or len(self.revision.encode("utf-8")) > 256
        ):
            raise ValueError(
                "revision must contain 1 to 256 UTF-8 bytes "
                "without surrounding whitespace"
            )

        if not isinstance(
            self.automatically_accept_compatible, bool
        ):
            raise ValueError(
                "automatically_accept_compatible must be a boolean"
            )


@dataclass(frozen=True)
class ContractDecision:
    action: ContractAction
    reason: str
    policy_revision: str


def decide_contract(
    *,
    assessment: ContractAssessment,
    policy: ContractPolicy,
) -> ContractDecision:
    if not isinstance(assessment, ContractAssessment):
        raise ValueError(
            "assessment must be a ContractAssessment"
        )

    if not isinstance(policy, ContractPolicy):
        raise ValueError("policy must be a ContractPolicy")

    if assessment.validation == ValidationOutcome.INVALID:
        action = ContractAction.BLOCK
        reason = "INVALID_CONTRACT"

    elif assessment.validation == ValidationOutcome.UNSUPPORTED:
        action = ContractAction.BLOCK
        reason = "VALIDATION_UNSUPPORTED"

    elif (
        assessment.compatibility
        == CompatibilityOutcome.UNSUPPORTED
    ):
        action = ContractAction.BLOCK
        reason = "COMPATIBILITY_UNSUPPORTED"

    elif (
        assessment.compatibility
        == CompatibilityOutcome.NO_CHANGE
    ):
        action = ContractAction.REUSE_EXISTING
        reason = "REUSE_APPROVED_BASELINE"

    elif (
        assessment.compatibility
        == CompatibilityOutcome.FIRST_CONTRACT
    ):
        action = ContractAction.REQUIRE_APPROVAL
        reason = "FIRST_CONTRACT_REQUIRES_APPROVAL"

    elif (
        assessment.compatibility
        == CompatibilityOutcome.BREAKING
    ):
        action = ContractAction.REQUIRE_APPROVAL
        reason = "BREAKING_CHANGE_REQUIRES_APPROVAL"

    elif policy.automatically_accept_compatible:
        action = ContractAction.ACCEPT_UNDER_POLICY
        reason = "COMPATIBLE_CHANGE_ACCEPTED_BY_POLICY"

    else:
        action = ContractAction.REQUIRE_APPROVAL
        reason = "COMPATIBLE_CHANGE_REQUIRES_APPROVAL"

    return ContractDecision(
        action=action,
        reason=reason,
        policy_revision=policy.revision,
    )