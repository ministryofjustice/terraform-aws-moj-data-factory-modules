from dataclasses import asdict

from ingestion_registry.contract_policy import ContractPolicy
from ingestion_registry.identity import TableIdentity
from ingestion_registry.references import S3ObjectReference


class ContractOrchestrationBlocked(ValueError):
    """The request cannot progress through the configured contract process."""


class ContractOrchestration:
    def __init__(
        self,
        *,
        lifecycle,
        lookup,
        assessment_admission,
        reuse,
        activation,
        trusted_policy: ContractPolicy,
        preparation=None,
        generated_assessment=None,
    ):
        if not isinstance(trusted_policy, ContractPolicy):
            raise ValueError("trusted_policy must be a ContractPolicy")

        registry = lifecycle.registry

        if (
            preparation is not None
            and preparation.registry is not registry
        ):
            raise ValueError(
                "Preparation and orchestration must use the same Registry"
            )

        if (
            generated_assessment is not None
            and generated_assessment.registry is not registry
        ):
            raise ValueError(
                "Generated assessment and orchestration "
                "must use the same Registry"
            )

        if (
            preparation is not None
            and generated_assessment is not None
            and preparation.completion is not generated_assessment.completion
        ):
            raise ValueError(
                "Preparation and assessment must use "
                "the same generation completion service"
            )

        if any(
            candidate is not registry
            for candidate in (
                lookup.registry,
                assessment_admission.lifecycle.registry,
                reuse.registry,
                activation.registry,
            )
        ):
            raise ValueError(
                "Contract orchestration services must use the same Registry"
            )

        self.lifecycle = lifecycle
        self.lookup = lookup
        self.assessment_admission = assessment_admission
        self.reuse = reuse
        self.activation = activation
        self.trusted_policy = trusted_policy
        self.preparation = preparation
        self.registry = registry
        self.generated_assessment = generated_assessment

    def prepare(
        self,
        *,
        table: TableIdentity,
        request_id: str,
    ) -> dict:
        if self.preparation is None:
            raise ContractOrchestrationBlocked(
                "Contract generation preparation is not configured"
            )

        return self.preparation.prepare(
            table=table,
            request_id=request_id,
        )

    def resolve(self, *, table: TableIdentity) -> dict:
        resolved = self.lookup.resolve(table=table)

        if resolved is None:
            return {
                "status": "CONTRACT_REQUIRED",
                "table_identity": asdict(table),
            }

        return self._ready(
            table=table,
            action="RESOLVED",
            selection=resolved["selection"],
        )

    def complete_assessment(
        self,
        *,
        table: TableIdentity,
        reservation_key: str,
        preparation_fingerprint: str,
        evidence_reference: S3ObjectReference,
    ) -> dict:
        if self.generated_assessment is None:
            raise ContractOrchestrationBlocked(
                "Generated contract assessment is not configured"
            )

        if not isinstance(table, TableIdentity):
            raise ValueError("table must be a TableIdentity")

        if not isinstance(evidence_reference, S3ObjectReference):
            raise ValueError(
                "evidence_reference must be an S3ObjectReference"
            )

        reservation = self.generated_assessment.completion._reservation(
            table,
            reservation_key,
            preparation_fingerprint,
        )

        generation = self.registry._read(
            reservation_key,
            "GENERATION",
        )

        if generation is None:
            raise ContractOrchestrationBlocked(
                "Generation completion must exist before assessment"
            )

        request = self.generated_assessment.submit(
            table=table,
            reservation_key=reservation_key,
            request_fingerprint=preparation_fingerprint,
            evidence_reference=evidence_reference,
            policy=self.trusted_policy,
        )

        if (
            request["request_id"] != reservation["request_id"]
            or self.registry._read(reservation_key, "META") != reservation
            or self.registry._read(reservation_key, "GENERATION")
            != generation
        ):
            raise ContractOrchestrationBlocked(
                "Generation context changed during assessment admission"
            )

        return self.advance(
            table=table,
            request_id=request["request_id"],
            contract_version=reservation["contract_version"],
            expected_request_fingerprint=request["request_fingerprint"],
        )

    def advance(
        self,
        *,
        table: TableIdentity,
        request_id: str,
        contract_version: str,
        expected_request_fingerprint: str,
    ) -> dict:
        self.lifecycle._key(table, request_id)

        if (
            not isinstance(expected_request_fingerprint, str)
            or len(expected_request_fingerprint) != 64
            or any(
                character not in "0123456789abcdef"
                for character in expected_request_fingerprint
            )
        ):
            raise ValueError("Invalid expected request fingerprint")

        request = self.lifecycle.read(
            table=table,
            request_id=request_id,
        )

        if (
            request is None
            or request.get("table_identity") != asdict(table)
            or request.get("request_id") != request_id
            or request.get("request_fingerprint")
            != expected_request_fingerprint
        ):
            raise ContractOrchestrationBlocked(
                "Operation does not match the exact contract request"
            )

        if request.get("policy") != asdict(self.trusted_policy):
            raise ContractOrchestrationBlocked(
                "Request policy does not match the configured trusted policy"
            )

        assessment = request["assessment"]

        verified = self.assessment_admission.submit(
            table=table,
            request_id=request_id,
            contract_version=contract_version,
            candidate_reference=S3ObjectReference(
                **assessment["candidate_reference"]
            ),
            evidence_reference=S3ObjectReference(
                **assessment["evidence_reference"]
            ),
            policy=self.trusted_policy,
        )

        if verified["request_fingerprint"] != expected_request_fingerprint:
            raise ContractOrchestrationBlocked(
                "Assessment changed during orchestration"
            )

        status = verified["status"]

        if status in {"BLOCKED", "REJECTED"}:
            return {
                "status": "BLOCKED",
                "request_id": request_id,
                "request_fingerprint": expected_request_fingerprint,
                "reason": (
                    "REVIEW_REJECTED"
                    if status == "REJECTED"
                    else verified["decision"]["reason"]
                ),
            }

        if status == "PENDING_REVIEW":
            return {
                "status": "REVIEW_REQUIRED",
                "request_id": request_id,
                "request_fingerprint": expected_request_fingerprint,
                "request_revision": int(verified["revision"]),
                "reason": verified["decision"]["reason"],
            }

        if status in {"REUSE_PENDING", "REUSED"}:
            result = self.reuse.complete(
                table=table,
                request_id=request_id,
                expected_request_fingerprint=expected_request_fingerprint,
            )

            return self._ready(
                table=table,
                action="REUSED",
                selection=result["selection"],
            )

        if status in {"APPROVED", "POLICY_ACCEPTED"}:
            result = self.activation.activate(
                table=table,
                request_id=request_id,
                contract_version=contract_version,
            )

            return self._ready(
                table=table,
                action="ACTIVATED",
                selection=result["selection"],
            )

        raise ContractOrchestrationBlocked(
            f"Unsupported contract lifecycle status: {status}"
        )

    @staticmethod
    def _ready(*, table, action, selection):
        registered = selection["registered_contract"]
        reference = registered["object_reference"]

        return {
            "status": "CONTRACT_READY",
            "action": action,
            "table_identity": asdict(table),
            "selection": selection,
            "s3_contract_uri": (
                f"s3://{reference['bucket_name']}/"
                f"{reference['object_key']}"
            ),
        }