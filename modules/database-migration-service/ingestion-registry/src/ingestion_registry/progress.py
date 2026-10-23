import copy
import hashlib
import json
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from botocore.exceptions import ClientError

from ingestion_registry.admission import IngestionRegistry
from ingestion_registry.ownership import (
    OwnershipClaim,
    OwnershipLost,
)


STAGES = frozenset(
    {
        "CONTRACT",
        "SOURCE_PREFLIGHT",
        "EXTRACTION",
        "PROCESSING",
        "HISTORY_BASELINE",
        "RECONCILIATION",
        "PUBLICATION",
        "CDC_PRODUCER",
        "CDC_CONSUMER",
        "HISTORY_CDC",
        "SOURCE_STOPPED",
        "CDC_DRAINED",
        "HISTORY_FINALIZED",
    }
)

STAGE_STATUSES = frozenset(
    {
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "BLOCKED",
    }
)


class ProgressConflict(RuntimeError):
    """The update conflicts with recorded stage progress."""


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


class IngestionProgress:
    def __init__(
        self,
        registry: IngestionRegistry,
        *,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.registry = registry
        self.clock = clock or (lambda: int(time.time()))

    def _guards(
        self,
        claim: OwnershipClaim,
        now: int,
    ) -> list[dict[str, Any]]:
        registry = self.registry
        partition_key = f"INGESTION#{claim.ingestion_id}"

        return [
            {
                "ConditionCheck": {
                    "TableName": registry.table_name,
                    "Key": registry._encode(
                        {
                            "pk": partition_key,
                            "sk": "OWNERSHIP",
                        }
                    ),
                    "ConditionExpression": (
                        "execution_id = :execution "
                        "AND fencing_token = :token "
                        "AND attempt = :attempt "
                        "AND lease_expires_at > :now"
                    ),
                    "ExpressionAttributeValues": registry._encode(
                        {
                            ":execution": claim.execution_id,
                            ":token": claim.fencing_token,
                            ":attempt": claim.attempt,
                            ":now": now,
                        }
                    ),
                }
            },
            {
                "ConditionCheck": {
                    "TableName": registry.table_name,
                    "Key": registry._encode(
                        {
                            "pk": partition_key,
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

    def read_stage(
        self,
        *,
        ingestion_id: str,
        stage: str,
    ) -> dict[str, Any] | None:
        if stage not in STAGES:
            raise ValueError(f"Unsupported stage: {stage!r}")

        return self.registry._read(
            f"INGESTION#{ingestion_id}",
            f"STAGE#{stage}",
        )

    def record_stage(
        self,
        claim: OwnershipClaim,
        *,
        stage: str,
        status: str,
        update_id: str,
        expected_revision: int,
        result_reference: dict[str, str] | None = None,
        error_summary: str | None = None,
    ) -> dict[str, Any]:
        if stage not in STAGES:
            raise ValueError(f"Unsupported stage: {stage!r}")

        if status not in STAGE_STATUSES:
            raise ValueError(f"Unsupported stage status: {status!r}")

        if (
            not isinstance(update_id, str)
            or not update_id
            or update_id != update_id.strip()
            or len(update_id.encode("utf-8")) > 256
        ):
            raise ValueError(
                "update_id must contain 1 to 256 UTF-8 bytes "
                "without surrounding whitespace"
            )

        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise ValueError(
                "expected_revision must be a non-negative integer"
            )

        reference = copy.deepcopy(result_reference)

        if reference is not None:
            if (
                not isinstance(reference, dict)
                or not reference
                or not all(
                    isinstance(key, str)
                    and bool(key.strip())
                    and isinstance(value, str)
                    and bool(value.strip())
                    for key, value in reference.items()
                )
                or len(
                    json.dumps(reference).encode("utf-8")
                ) > 8192
            ):
                raise ValueError(
                    "result_reference must be a non-empty string map "
                    "no larger than 8 KiB"
                )

        if status == "SUCCEEDED" and reference is None:
            raise ValueError(
                "A successful stage requires a durable result_reference"
            )

        if status in {"FAILED", "BLOCKED"}:
            if (
                not isinstance(error_summary, str)
                or not error_summary.strip()
                or len(error_summary.encode("utf-8")) > 2048
            ):
                raise ValueError(
                    "Failed or blocked stages require an error_summary "
                    "no larger than 2 KiB"
                )
        elif error_summary is not None:
            raise ValueError(
                "error_summary is only supported for FAILED or BLOCKED"
            )

        now = self.clock()

        if (
            not isinstance(now, int)
            or isinstance(now, bool)
            or now < 0
        ):
            raise ValueError(
                "Registry clock must return non-negative epoch seconds"
            )

        registry = self.registry
        partition_key = f"INGESTION#{claim.ingestion_id}"
        update_key = f"STAGEUPDATE#{_digest(update_id)}"

        payload = {
            "stage": stage,
            "status": status,
            "expected_revision": expected_revision,
            "result_reference": reference,
            "error_summary": error_summary,
        }
        fingerprint = _digest(payload)

        previous_update = registry._read(
            partition_key,
            update_key,
        )

        if previous_update is not None:
            if previous_update["update_fingerprint"] != fingerprint:
                raise ProgressConflict(
                    "The update ID is already recorded "
                    "with different content"
                )

            self._commit(self._guards(claim, now))
            return copy.deepcopy(previous_update["result"])

        existing = self.read_stage(
            ingestion_id=claim.ingestion_id,
            stage=stage,
        )

        actual_revision = (
            int(existing["revision"])
            if existing is not None
            else 0
        )

        if actual_revision != expected_revision:
            raise ProgressConflict(
                "Stage revision changed; read and reconcile "
                "the current stage before updating it"
            )

        if existing is not None and existing["status"] == "SUCCEEDED":
            raise ProgressConflict(
                "A completed stage cannot be overwritten"
            )

        record = {
            "pk": partition_key,
            "sk": f"STAGE#{stage}",
            "record_type": "STAGE",
            "ingestion_id": claim.ingestion_id,
            "stage": stage,
            "status": status,
            "revision": expected_revision + 1,
            "execution_id": claim.execution_id,
            "attempt": claim.attempt,
            "fencing_token": claim.fencing_token,
            "update_id": update_id,
            "result_reference": reference,
            "error_summary": error_summary,
            "updated_at": datetime.fromtimestamp(
                now,
                tz=timezone.utc,
            ).isoformat(),
        }

        stage_put = {
            "TableName": registry.table_name,
            "Item": registry._encode(record),
            "ConditionExpression": "attribute_not_exists(pk)",
        }

        if expected_revision > 0:
            stage_put["ConditionExpression"] = "#revision = :expected"
            stage_put["ExpressionAttributeNames"] = {
                "#revision": "revision",
            }
            stage_put["ExpressionAttributeValues"] = registry._encode(
                {":expected": expected_revision}
            )

        update_record = {
            "pk": partition_key,
            "sk": update_key,
            "record_type": "STAGE_UPDATE",
            "update_id": update_id,
            "update_fingerprint": fingerprint,
            "result": record,
        }

        self._commit(
            self._guards(claim, now)
            + [
                {"Put": stage_put},
                registry._put_new(update_record),
            ]
        )

        return copy.deepcopy(record)

    def _commit(
        self,
        actions: list[dict[str, Any]],
    ) -> None:
        try:
            self.registry.client.transact_write_items(
                TransactItems=actions,
                ClientRequestToken=str(uuid4()),
            )
        except ClientError as error:
            if (
                error.response["Error"]["Code"]
                != "TransactionCanceledException"
            ):
                raise

            reasons = error.response.get("CancellationReasons", [])

            if any(
                reason.get("Code") == "ConditionalCheckFailed"
                for reason in reasons[:2]
            ):
                raise OwnershipLost(
                    "Progress update rejected because ownership "
                    "is no longer valid"
                ) from error

            if any(
                reason.get("Code") == "ConditionalCheckFailed"
                for reason in reasons[2:]
            ):
                raise ProgressConflict(
                    "Progress changed concurrently; retry the same "
                    "update or reconcile the recorded stage"
                ) from error

            raise