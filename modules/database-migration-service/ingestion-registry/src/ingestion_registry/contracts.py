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
        approval_reference: S3ObjectReference,
        update_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        if not isinstance(approval_reference, S3ObjectReference):
            raise ValueError(
                "approval_reference must be an S3ObjectReference"
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
            "approval_bucket_name": approval_reference.bucket_name,
            "approval_object_key": approval_reference.object_key,
            "approval_object_version_id": approval_reference.version_id,
            "approval_object_sha256": approval_reference.sha256,
        }

        return self.progress.record_stage(
            claim,
            stage="CONTRACT",
            status="SUCCEEDED",
            update_id=update_id,
            expected_revision=expected_revision,
            result_reference=reference,
        )