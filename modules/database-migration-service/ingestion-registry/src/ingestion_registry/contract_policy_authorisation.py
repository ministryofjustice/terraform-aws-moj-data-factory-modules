import copy
from dataclasses import asdict
from ingestion_registry.references import S3ObjectReference

from ingestion_registry.contract_policy import ContractPolicy


class ContractPolicyAuthorisationBlocked(ValueError):
    """Policy acceptance does not have matching trusted authorisation."""


class ContractPolicyAuthorisation:
    def __init__(
        self,
        *,
        assessment_admission,
        trusted_policy: ContractPolicy,
    ):
        if not isinstance(trusted_policy, ContractPolicy):
            raise ValueError(
                "trusted_policy must be a ContractPolicy"
            )

        self.admission = assessment_admission
        self.lifecycle = assessment_admission.lifecycle
        self.registry = self.lifecycle.registry
        self.trusted_policy = trusted_policy

    def verify(
        self,
        *,
        table,
        request_id: str,
        contract_version: str,
    ) -> dict:
        request = self.lifecycle.read(
            table=table,
            request_id=request_id,
        )

        if request is None:
            raise ContractPolicyAuthorisationBlocked(
                "Contract request does not exist"
            )

        if (
            request.get("table_identity") != asdict(table)
            or request.get("request_id") != request_id
            or request.get("status") != "POLICY_ACCEPTED"
        ):
            raise ContractPolicyAuthorisationBlocked(
                "Contract request is not accepted under policy"
            )

        if not self.trusted_policy.automatically_accept_compatible:
            raise ContractPolicyAuthorisationBlocked(
                "Automatic compatible-change acceptance is disabled"
            )

        stored_policy = request.get("policy")

        try:
            recorded_policy = ContractPolicy(**stored_policy)
        except (TypeError, ValueError) as error:
            raise ContractPolicyAuthorisationBlocked(
                "Recorded contract policy is invalid"
            ) from error

        if recorded_policy != self.trusted_policy:
            raise ContractPolicyAuthorisationBlocked(
                "Recorded policy does not match the trusted policy"
            )

        assessment = request.get("assessment")
        decision = request.get("decision")

        if (
            not isinstance(assessment, dict)
            or assessment.get("validation") != "VALID"
            or assessment.get("compatibility") != "COMPATIBLE"
            or assessment.get("baseline_reference") is None
            or not isinstance(decision, dict)
            or decision
            != {
                "action": "ACCEPT_UNDER_POLICY",
                "reason": "COMPATIBLE_CHANGE_ACCEPTED_BY_POLICY",
                "policy_revision": self.trusted_policy.revision,
            }
        ):
            raise ContractPolicyAuthorisationBlocked(
                "Policy acceptance requires a validated compatible change"
            )


        try:
            candidate_reference = S3ObjectReference(
                **assessment["candidate_reference"]
            )
            evidence_reference = S3ObjectReference(
                **assessment["evidence_reference"]
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ContractPolicyAuthorisationBlocked(
                "Policy acceptance has invalid evidence references"
            ) from error

        verified_request = self.admission.submit(
            table=table,
            request_id=request_id,
            contract_version=contract_version,
            candidate_reference=candidate_reference,
            evidence_reference=evidence_reference,
            policy=self.trusted_policy,
        )

        if verified_request != request:
            raise ContractPolicyAuthorisationBlocked(
                "Contract assessment changed during policy verification"
            )

        latest = self.lifecycle.read(
            table=table,
            request_id=request_id,
        )

        if latest != request:
            raise ContractPolicyAuthorisationBlocked(
                "Contract request changed during policy verification"
            )

        return {
            "authorisation_kind": "POLICY",
            "request_key": request["pk"],
            "request_id": request_id,
            "request_fingerprint": request["request_fingerprint"],
            "expected_revision": int(request["revision"]),
            "table_identity": copy.deepcopy(request["table_identity"]),
            "assessment": copy.deepcopy(assessment),
            "policy": asdict(self.trusted_policy),
            "decision": copy.deepcopy(decision),
            "evidence_reference": asdict(evidence_reference),
        }