import copy
import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone

from botocore.exceptions import ClientError

from ingestion_registry.admission import IngestionRegistry
from ingestion_registry.contract_policy import (
    ContractAction,
    ContractAssessment,
    ContractPolicy,
    decide_contract,
)
from ingestion_registry.identity import TableIdentity, identity_digest


class ContractRequestConflict(ValueError):
    """A contract request ID was reused with different content."""


class ContractLifecycle:
    def __init__(self, registry: IngestionRegistry) -> None:
        self.registry = registry

    @staticmethod
    def _key(table: TableIdentity, request_id: str) -> str:
        if not isinstance(table, TableIdentity):
            raise ValueError("table must be a TableIdentity")

        if (
            not isinstance(request_id, str)
            or not request_id
            or request_id != request_id.strip()
            or len(request_id.encode("utf-8")) > 256
        ):
            raise ValueError(
                "request_id must contain 1 to 256 UTF-8 bytes "
                "without surrounding whitespace"
            )

        return "CONTRACTREQUEST#" + identity_digest(
            [
                "contract-request-v1",
                table.partition_key,
                request_id,
            ]
        )

    def read(
        self,
        *,
        table: TableIdentity,
        request_id: str,
    ) -> dict | None:
        return self.registry._read(
            self._key(table, request_id),
            "META",
        )

    @staticmethod
    def _existing(existing: dict, fingerprint: str) -> dict:
        if existing.get("request_fingerprint") != fingerprint:
            raise ContractRequestConflict(
                "Contract request ID is already recorded "
                "with different content"
            )

        return copy.deepcopy(existing)

    def submit_assessment(
        self,
        *,
        table: TableIdentity,
        request_id: str,
        assessment: ContractAssessment,
        policy: ContractPolicy,
    ) -> dict:
        partition_key = self._key(table, request_id)
        decision = decide_contract(
            assessment=assessment,
            policy=policy,
        )

        payload = {
            "table_identity": asdict(table),
            "request_id": request_id,
            "assessment": asdict(assessment),
            "policy": asdict(policy),
            "decision": asdict(decision),
        }

        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        payload = json.loads(encoded)
        fingerprint = hashlib.sha256(
            encoded.encode("utf-8")
        ).hexdigest()

        existing = self.registry._read(partition_key, "META")

        if existing is not None:
            return self._existing(existing, fingerprint)

        statuses = {
            ContractAction.BLOCK: "BLOCKED",
            ContractAction.REUSE_EXISTING: "REUSE_PENDING",
            ContractAction.REQUIRE_APPROVAL: "PENDING_REVIEW",
            ContractAction.ACCEPT_UNDER_POLICY: "POLICY_ACCEPTED",
        }

        now = datetime.now(timezone.utc).isoformat()
        record = {
            "pk": partition_key,
            "sk": "META",
            "record_type": "CONTRACT_REQUEST",
            "request_fingerprint": fingerprint,
            **payload,
            "status": statuses[decision.action],
            "revision": 1,
            "created_at": now,
            "updated_at": now,
        }

        action = self.registry._put_new(record)["Put"]

        try:
            self.registry.client.put_item(**action)
        except ClientError as error:
            if (
                error.response["Error"]["Code"]
                != "ConditionalCheckFailedException"
            ):
                raise

            existing = self.registry._read(partition_key, "META")

            if existing is None:
                raise RuntimeError(
                    "Concurrent contract request could not be read; "
                    "retry using the same request ID and content"
                ) from error

            return self._existing(existing, fingerprint)

        return copy.deepcopy(record)