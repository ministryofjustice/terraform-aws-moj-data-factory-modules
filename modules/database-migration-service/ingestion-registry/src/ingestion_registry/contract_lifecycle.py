import copy
import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from ingestion_registry.references import S3ObjectReference

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

class ContractReviewConflict(ValueError):
    """The review conflicts with the current request or an earlier review."""


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

    def record_review(
        self,
        *,
        table: TableIdentity,
        request_id: str,
        review_id: str,
        reviewer: str,
        outcome: str,
        expected_revision: int,
        expected_request_fingerprint: str,
        evidence_reference: S3ObjectReference,
        authorise,
    ) -> dict:
        partition_key = self._key(table, request_id)

        for field, value in (
            ("review_id", review_id),
            ("reviewer", reviewer),
        ):
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or len(value.encode("utf-8")) > 256
            ):
                raise ValueError(
                    f"{field} must contain 1 to 256 UTF-8 bytes "
                    "without surrounding whitespace"
                )

        if outcome not in {"APPROVED", "REJECTED"}:
            raise ValueError("outcome must be APPROVED or REJECTED")

        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 1
        ):
            raise ValueError(
                "expected_revision must be a positive integer"
            )

        if (
            not isinstance(expected_request_fingerprint, str)
            or len(expected_request_fingerprint) != 64
            or any(
                character not in "0123456789abcdef"
                for character in expected_request_fingerprint
            )
        ):
            raise ValueError(
                "expected_request_fingerprint must contain "
                "64 lowercase hexadecimal characters"
            )

        if not isinstance(evidence_reference, S3ObjectReference):
            raise ValueError(
                "evidence_reference must be an S3ObjectReference"
            )

        if not callable(authorise):
            raise ValueError("authorise must be callable")

        if authorise(reviewer, table) is not None:
            raise ValueError(
                "authorise must raise on denial and return None on success"
            )

        review_key = "REVIEW#" + identity_digest(
            ["contract-review-v1", review_id]
        )

        payload = {
            "review_id": review_id,
            "reviewer": reviewer,
            "outcome": outcome,
            "expected_revision": expected_revision,
            "expected_request_fingerprint": (
                expected_request_fingerprint
            ),
            "evidence_reference": asdict(evidence_reference),
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

        def existing_review() -> dict | None:
            existing = self.registry._read(
                partition_key, review_key
            )

            if existing is None:
                return None

            if existing.get("review_fingerprint") != fingerprint:
                raise ContractReviewConflict(
                    "Review ID is already recorded with different content"
                )

            return copy.deepcopy(existing["result"])

        previous = existing_review()

        if previous is not None:
            return previous

        current = self.registry._read(partition_key, "META")

        if current is None:
            raise ContractReviewConflict(
                "Contract request does not exist"
            )

        if (
            current.get("request_fingerprint")
            != expected_request_fingerprint
        ):
            raise ContractReviewConflict(
                "Review does not match the exact assessment request"
            )

        if int(current["revision"]) != expected_revision:
            raise ContractReviewConflict(
                "Request revision changed; read the current request"
            )

        if current["status"] != "PENDING_REVIEW":
            raise ContractReviewConflict(
                "Only a pending review can be approved or rejected"
            )

        now = datetime.now(timezone.utc).isoformat()
        updated = copy.deepcopy(current)
        updated.update(
            {
                "status": outcome,
                "revision": expected_revision + 1,
                "review": payload,
                "updated_at": now,
            }
        )

        review_record = {
            "pk": partition_key,
            "sk": review_key,
            "record_type": "CONTRACT_REVIEW",
            "review_fingerprint": fingerprint,
            **payload,
            "recorded_at": now,
            "result": updated,
        }

        try:
            self.registry.client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.registry.table_name,
                            "Item": self.registry._encode(updated),
                            "ConditionExpression": (
                                "#revision = :revision "
                                "AND #status = :pending "
                                "AND request_fingerprint = :fingerprint"
                            ),
                            "ExpressionAttributeNames": {
                                "#revision": "revision",
                                "#status": "status",
                            },
                            "ExpressionAttributeValues": (
                                self.registry._encode(
                                    {
                                        ":revision": expected_revision,
                                        ":pending": "PENDING_REVIEW",
                                        ":fingerprint": (
                                            expected_request_fingerprint
                                        ),
                                    }
                                )
                            ),
                        }
                    },
                    self.registry._put_new(review_record),
                ]
            )
        except ClientError as error:
            if (
                error.response["Error"]["Code"]
                != "TransactionCanceledException"
            ):
                raise

            previous = existing_review()

            if previous is not None:
                return previous

            reasons = error.response.get(
                "CancellationReasons", []
            )
            if any(
                reason.get("Code") == "ConditionalCheckFailed"
                for reason in reasons
            ):
                raise ContractReviewConflict(
                    "Review state changed concurrently; "
                    "read and reconcile the current request"
                ) from error

            raise

        return copy.deepcopy(updated)