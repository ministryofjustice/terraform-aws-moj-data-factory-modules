import re
import uuid
from dataclasses import dataclass
from typing import Any

from ingestion_registry.ownership import OwnershipClaim
from ingestion_registry.progress import IngestionProgress
from ingestion_registry.references import S3ObjectReference


@dataclass(frozen=True)
class RegisteredContract:
    contract_id: str
    contract_version: str
    contract_fingerprint: str
    object_reference: S3ObjectReference

    def __post_init__(self) -> None:
        if not isinstance(self.object_reference, S3ObjectReference):
            raise ValueError(
                "object_reference must be an S3ObjectReference"
            )

        if (
            not isinstance(self.contract_version, str)
            or re.fullmatch(
                r"v[1-9][0-9]*",
                self.contract_version,
            ) is None
        ):
            raise ValueError(
                "contract_version must use v1, v2, v3, etc."
            )

        if (
            not isinstance(self.contract_fingerprint, str)
            or re.fullmatch(
                r"[a-f0-9]{64}",
                self.contract_fingerprint,
            ) is None
        ):
            raise ValueError(
                "contract_fingerprint must contain "
                "64 lowercase hexadecimal characters"
            )

@dataclass(frozen=True)
class PolicyContractAuthorisation:
    evidence_reference: S3ObjectReference
    policy_revision: str
    request_key: str
    request_fingerprint: str
    request_revision: int

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_reference, S3ObjectReference):
            raise ValueError(
                "evidence_reference must be an S3ObjectReference"
            )

        if (
            not isinstance(self.policy_revision, str)
            or not self.policy_revision
            or self.policy_revision != self.policy_revision.strip()
            or len(self.policy_revision.encode("utf-8")) > 256
        ):
            raise ValueError(
                "policy_revision must contain 1 to 256 UTF-8 bytes "
                "without surrounding whitespace"
            )

        if (
            not isinstance(self.request_key, str)
            or re.fullmatch(
                r"CONTRACTREQUEST#[a-f0-9]{64}",
                self.request_key,
            ) is None
        ):
            raise ValueError("Invalid policy authorisation request_key")

        if (
            not isinstance(self.request_fingerprint, str)
            or re.fullmatch(
                r"[a-f0-9]{64}",
                self.request_fingerprint,
            ) is None
        ):
            raise ValueError(
                "request_fingerprint must contain "
                "64 lowercase hexadecimal characters"
            )

        if (
            not isinstance(self.request_revision, int)
            or isinstance(self.request_revision, bool)
            or self.request_revision < 1
        ):
            raise ValueError(
                "request_revision must be a positive integer"
            )

class IngestionContracts:
    def __init__(
        self,
        progress: IngestionProgress,
    ) -> None:
        self.progress = progress

    def pin(
        self,
        claim: OwnershipClaim,
        *,
        contract: RegisteredContract,
        update_id: str,
        expected_revision: int,
        approval_reference: S3ObjectReference | None = None,
        policy_authorisation: PolicyContractAuthorisation | None = None,
        condition_checks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if policy_authorisation is None:
            if not isinstance(approval_reference, S3ObjectReference):
                raise ValueError(
                    "approval_reference must be an S3ObjectReference "
                    "when policy_authorisation is absent"
                )
        else:
            if not isinstance(
                policy_authorisation,
                PolicyContractAuthorisation,
            ):
                raise ValueError(
                    "policy_authorisation must be a "
                    "PolicyContractAuthorisation"
                )

            if approval_reference is not None:
                raise ValueError(
                    "Provide either approval_reference or "
                    "policy_authorisation, not both"
                )

        ingestion = self.progress.registry._read(
            f"INGESTION#{claim.ingestion_id}",
            "META",
        )

        if ingestion is None:
            raise ValueError("Ingestion does not exist")

        identity = ingestion["table_identity"]

        expected_contract_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_DNS,
                ":".join(
                    identity[field]
                    for field in (
                        "source",
                        "database_name",
                        "schema_name",
                        "table_name",
                    )
                ),
            )
        )

        if contract.contract_id != expected_contract_id:
            raise ValueError(
                "contract_id does not match the ingestion table identity"
            )

        expected_key = "/".join(
            [
                identity["source"],
                identity["database_name"],
                identity["schema_name"],
                identity["table_name"],
                contract.contract_version,
                "contract.avsc",
            ]
        )

        if contract.object_reference.object_key != expected_key:
            raise ValueError(
                "Contract object key does not match its table "
                "identity and contract version"
            )

        reference = {
            "kind": "registered-contract",
            "contract_id": contract.contract_id,
            "contract_version": contract.contract_version,
            "contract_fingerprint": contract.contract_fingerprint,
            "contract_bucket_name": (
                contract.object_reference.bucket_name
            ),
            "contract_object_key": contract.object_reference.object_key,
            "contract_object_version_id": (
                contract.object_reference.version_id
            ),
            "contract_object_sha256": contract.object_reference.sha256,
        }

        if policy_authorisation is None:
            # Preserve the existing human-approved result exactly.
            reference.update(
                {
                    "approval_bucket_name": approval_reference.bucket_name,
                    "approval_object_key": approval_reference.object_key,
                    "approval_object_version_id": (
                        approval_reference.version_id
                    ),
                    "approval_object_sha256": approval_reference.sha256,
                }
            )
        else:
            evidence = policy_authorisation.evidence_reference

            reference.update(
                {
                    "authorisation_kind": "POLICY",
                    "authorisation_bucket_name": evidence.bucket_name,
                    "authorisation_object_key": evidence.object_key,
                    "authorisation_object_version_id": evidence.version_id,
                    "authorisation_object_sha256": evidence.sha256,
                    "policy_revision": policy_authorisation.policy_revision,
                    "authorisation_request_key": (
                        policy_authorisation.request_key
                    ),
                    "authorisation_request_fingerprint": (
                        policy_authorisation.request_fingerprint
                    ),
                    "authorisation_request_revision": str(
                        policy_authorisation.request_revision
                    ),
                }
            )

        return self.progress.record_stage(
            claim,
            stage="CONTRACT",
            status="SUCCEEDED",
            update_id=update_id,
            expected_revision=expected_revision,
            result_reference=reference,
            condition_checks=condition_checks,
        )