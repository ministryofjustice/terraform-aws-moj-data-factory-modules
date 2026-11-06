from ingestion_registry.contract_policy import ContractPolicy
from ingestion_registry.identity import TableIdentity
from ingestion_registry.references import S3ObjectReference


class GeneratedContractAssessmentBlocked(ValueError):
    """Assessment requires a matching recorded generation completion."""


class GeneratedContractAssessment:
    def __init__(
        self,
        *,
        generation_completion,
        assessment_admission,
    ):
        if (
            generation_completion.registry
            is not assessment_admission.lifecycle.registry
        ):
            raise ValueError(
                "Generation and assessment must use the same Registry"
            )

        self.completion = generation_completion
        self.admission = assessment_admission
        self.registry = generation_completion.registry

    def submit(
        self,
        *,
        table: TableIdentity,
        reservation_key: str,
        request_fingerprint: str,
        evidence_reference: S3ObjectReference,
        policy: ContractPolicy,
    ) -> dict:
        if not isinstance(table, TableIdentity):
            raise ValueError("table must be a TableIdentity")

        if not isinstance(evidence_reference, S3ObjectReference):
            raise ValueError(
                "evidence_reference must be an S3ObjectReference"
            )

        if not isinstance(policy, ContractPolicy):
            raise ValueError("policy must be a ContractPolicy")

        reservation = self.completion._reservation(
            table,
            reservation_key,
            request_fingerprint,
        )

        recorded = self.registry._read(
            reservation_key,
            "GENERATION",
        )

        if recorded is None:
            raise GeneratedContractAssessmentBlocked(
                "Generation completion must be recorded before assessment"
            )

        generated = self.completion._verify_record(
            recorded,
            table,
            reservation,
        )

        return self.admission.submit(
            table=table,
            request_id=generated["request_id"],
            contract_version=generated["contract_version"],
            candidate_reference=S3ObjectReference(
                **generated["candidate_reference"]
            ),
            evidence_reference=evidence_reference,
            policy=policy,
        )