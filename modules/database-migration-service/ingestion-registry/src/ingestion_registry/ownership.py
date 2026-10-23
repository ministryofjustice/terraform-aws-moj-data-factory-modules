import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from botocore.exceptions import ClientError

from ingestion_registry.admission import IngestionRegistry


CLAIMABLE_STATUSES = frozenset(
    {
        "CREATED",
        "RUNNING",
        "FAILED",
        "RECOVERING",
    }
)


class OwnershipConflict(RuntimeError):
    """Another execution currently owns the ingestion."""


class OwnershipUnavailable(RuntimeError):
    """The ingestion cannot currently be claimed."""


class OwnershipLost(RuntimeError):
    """The execution no longer holds valid ownership."""


@dataclass(frozen=True)
class OwnershipClaim:
    ingestion_id: str
    execution_id: str
    fencing_token: int
    attempt: int
    lease_expires_at: int
    recovery_required: bool


class IngestionOwnership:
    def __init__(
        self,
        registry: IngestionRegistry,
        *,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.registry = registry
        self.clock = clock or (lambda: int(time.time()))

    @staticmethod
    def _result(
        ingestion_id: str,
        record: dict[str, Any],
    ) -> OwnershipClaim:
        return OwnershipClaim(
            ingestion_id=ingestion_id,
            execution_id=record["execution_id"],
            fencing_token=int(record["fencing_token"]),
            attempt=int(record["attempt"]),
            lease_expires_at=int(record["lease_expires_at"]),
            recovery_required=record["recovery_required"],
        )

    def claim(
        self,
        *,
        ingestion_id: str,
        execution_id: str,
        lease_seconds: int = 300,
    ) -> OwnershipClaim:
        """Claim work using the platform clock not request timestamps."""
        for field, value in (
            ("ingestion_id", ingestion_id),
            ("execution_id", execution_id),
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

        if (
            not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or not 1 <= lease_seconds <= 3600
        ):
            raise ValueError(
                "lease_seconds must be an integer from 1 to 3600"
            )

        registry = self.registry
        ingestion_pk = f"INGESTION#{ingestion_id}"

        for _ in range(3):
            now = self.clock()

            if (
                not isinstance(now, int)
                or isinstance(now, bool)
                or now < 0
            ):
                raise ValueError(
                    "Registry clock must return non-negative epoch seconds"
                )

            ingestion = registry._read(ingestion_pk, "META")

            if ingestion is None:
                raise OwnershipUnavailable("Ingestion does not exist")

            if ingestion["status"] not in CLAIMABLE_STATUSES:
                raise OwnershipUnavailable(
                    "Ingestion status does not permit an ownership claim"
                )

            existing = registry._read(ingestion_pk, "OWNERSHIP")

            if existing is not None:
                if int(existing["lease_expires_at"]) > now:
                    if existing["execution_id"] == execution_id:
                        # A retried claim returns the original token
                        # Renewal is a separate operation
                        return self._result(ingestion_id, existing)

                    raise OwnershipConflict(
                        "Another execution owns the ingestion"
                    )

                previous_token = int(existing["fencing_token"])
            else:
                previous_token = 0

            previous_attempt = int(ingestion["attempt_count"])
            attempt = previous_attempt + 1
            token = previous_token + 1
            expires = now + lease_seconds
            timestamp = datetime.fromtimestamp(
                now,
                tz=timezone.utc,
            ).isoformat()

            ownership = {
                "pk": ingestion_pk,
                "sk": "OWNERSHIP",
                "record_type": "OWNERSHIP",
                "execution_id": execution_id,
                "fencing_token": token,
                "attempt": attempt,
                "lease_expires_at": expires,
                "recovery_required": existing is not None,
                "updated_at": timestamp,
            }

            ownership_put = {
                "TableName": registry.table_name,
                "Item": registry._encode(ownership),
            }

            if existing is None:
                ownership_put["ConditionExpression"] = (
                    "attribute_not_exists(pk)"
                )
            else:
                ownership_put["ConditionExpression"] = (
                    "fencing_token = :previous "
                    "AND lease_expires_at <= :now"
                )
                ownership_put["ExpressionAttributeValues"] = (
                    registry._encode(
                        {
                            ":previous": previous_token,
                            ":now": now,
                        }
                    )
                )

            attempt_record = {
                "pk": ingestion_pk,
                "sk": f"ATTEMPT#{attempt:020d}",
                "record_type": "ATTEMPT",
                "execution_id": execution_id,
                "fencing_token": token,
                "attempt": attempt,
                "recovery_required": existing is not None,
                "created_at": timestamp,
            }

            try:
                registry.client.transact_write_items(
                    TransactItems=[
                        {"Put": ownership_put},
                        {
                            "Update": {
                                "TableName": registry.table_name,
                                "Key": registry._encode(
                                    {
                                        "pk": ingestion_pk,
                                        "sk": "META",
                                    }
                                ),
                                "UpdateExpression": (
                                    "SET attempt_count = :attempt, "
                                    "active_execution_id = :execution, "
                                    "updated_at = :updated"
                                ),
                                "ConditionExpression": (
                                    "attribute_exists(pk) "
                                    "AND attempt_count = :previous_attempt "
                                    "AND #status IN "
                                    "(:created, :running, :failed, :recovering)"
                                ),
                                "ExpressionAttributeNames": {
                                    "#status": "status",
                                },
                                "ExpressionAttributeValues": registry._encode(
                                    {
                                        ":attempt": attempt,
                                        ":execution": execution_id,
                                        ":updated": timestamp,
                                        ":previous_attempt": previous_attempt,
                                        ":created": "CREATED",
                                        ":running": "RUNNING",
                                        ":failed": "FAILED",
                                        ":recovering": "RECOVERING",
                                    }
                                ),
                            }
                        },
                        registry._put_new(attempt_record),
                    ],
                    ClientRequestToken=str(uuid4()),
                )

                return self._result(ingestion_id, ownership)

            except ClientError as error:
                if (
                    error.response["Error"]["Code"]
                    != "TransactionCanceledException"
                ):
                    raise

                reasons = {
                    reason.get("Code")
                    for reason in error.response.get(
                        "CancellationReasons",
                        [],
                    )
                }

                if not reasons or not reasons <= {
                    None,
                    "None",
                    "ConditionalCheckFailed",
                    "TransactionConflict",
                }:
                    raise

        raise OwnershipUnavailable(
            "Ownership changed during the claim. "
            "Retry after reconciling current ingestion state."
        )

    def renew(
        self,
        claim: OwnershipClaim,
        *,
        lease_seconds: int = 300,
    ) -> OwnershipClaim:
        """Extend current ownership without creating another attempt."""
        if (
            not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or not 1 <= lease_seconds <= 3600
        ):
            raise ValueError(
                "lease_seconds must be an integer from 1 to 3600"
            )

        registry = self.registry
        ingestion_pk = f"INGESTION#{claim.ingestion_id}"

        for _ in range(3):
            now = self.clock()

            if (
                not isinstance(now, int)
                or isinstance(now, bool)
                or now < 0
            ):
                raise ValueError(
                    "Registry clock must return non-negative epoch seconds"
                )

            ingestion = registry._read(ingestion_pk, "META")
            existing = registry._read(ingestion_pk, "OWNERSHIP")

            if (
                ingestion is None
                or existing is None
                or ingestion["status"] not in CLAIMABLE_STATUSES
                or ingestion.get("active_execution_id")
                != claim.execution_id
                or int(ingestion["attempt_count"]) != claim.attempt
                or existing["execution_id"] != claim.execution_id
                or int(existing["fencing_token"]) != claim.fencing_token
                or int(existing["attempt"]) != claim.attempt
                or int(existing["lease_expires_at"]) <= now
            ):
                raise OwnershipLost(
                    "The execution no longer holds an active lease"
                )

            previous_expiry = int(existing["lease_expires_at"])
            expires = max(
                previous_expiry,
                now + lease_seconds,
            )
            timestamp = datetime.fromtimestamp(
                now,
                tz=timezone.utc,
            ).isoformat()

            transaction: list[dict[str, Any]] = [
                {
                    "Update": {
                        "TableName": registry.table_name,
                        "Key": registry._encode(
                            {
                                "pk": ingestion_pk,
                                "sk": "OWNERSHIP",
                            }
                        ),
                        "UpdateExpression": (
                            "SET lease_expires_at = :expires, "
                            "updated_at = :updated"
                        ),
                        "ConditionExpression": (
                            "execution_id = :execution "
                            "AND fencing_token = :token "
                            "AND attempt = :attempt "
                            "AND lease_expires_at = :previous_expiry "
                            "AND lease_expires_at > :now"
                        ),
                        "ExpressionAttributeValues": registry._encode(
                            {
                                ":execution": claim.execution_id,
                                ":token": claim.fencing_token,
                                ":attempt": claim.attempt,
                                ":previous_expiry": previous_expiry,
                                ":expires": expires,
                                ":now": now,
                                ":updated": timestamp,
                            }
                        ),
                    }
                },
                {
                    "ConditionCheck": {
                        "TableName": registry.table_name,
                        "Key": registry._encode(
                            {
                                "pk": ingestion_pk,
                                "sk": "META",
                            }
                        ),
                        "ConditionExpression": (
                            "active_execution_id = :execution "
                            "AND attempt_count = :attempt "
                            "AND #status IN "
                            "(:created, :running, :failed, :recovering)"
                        ),
                        "ExpressionAttributeNames": {
                            "#status": "status",
                        },
                        "ExpressionAttributeValues": registry._encode(
                            {
                                ":execution": claim.execution_id,
                                ":attempt": claim.attempt,
                                ":created": "CREATED",
                                ":running": "RUNNING",
                                ":failed": "FAILED",
                                ":recovering": "RECOVERING",
                            }
                        ),
                    }
                },
            ]

            try:
                registry.client.transact_write_items(
                    TransactItems=transaction,
                    ClientRequestToken=str(uuid4()),
                )

                renewed = {
                    **existing,
                    "lease_expires_at": expires,
                    "updated_at": timestamp,
                }

                return self._result(
                    claim.ingestion_id,
                    renewed,
                )

            except ClientError as error:
                if (
                    error.response["Error"]["Code"]
                    != "TransactionCanceledException"
                ):
                    raise

                reasons = {
                    reason.get("Code")
                    for reason in error.response.get(
                        "CancellationReasons",
                        [],
                    )
                }

                if not reasons or not reasons <= {
                    None,
                    "None",
                    "ConditionalCheckFailed",
                    "TransactionConflict",
                }:
                    raise

        raise OwnershipUnavailable(
            "Ownership changed during renewal. "
            "Reconcile the current lease before continuing."
        )