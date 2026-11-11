import time
from collections.abc import Callable
from typing import Any

from dms_runtime.batch_handler import BatchHandler
from ingestion_registry.admission import IngestionRegistry
from ingestion_registry.ownership import OwnershipClaim, OwnershipLost


def build_batch_entry_point(
    *,
    handler: BatchHandler,
    registry: IngestionRegistry,
    clock: Callable[[], int] | None = None,
) -> Callable[[Any, Any], dict[str, str]]:
    """Build the JSON entry point for the downstream batch handler."""
    now = clock if clock is not None else lambda: int(time.time())

    def entry_point(event: Any, _context: Any) -> dict[str, str]:
        if not isinstance(event, dict) or set(event) != {
            "request_version",
            "action",
            "ingestion_id",
            "ingestion_owner",
        }:
            raise ValueError(
                "Batch events require request_version, action, "
                "ingestion_id and ingestion_owner"
            )

        version = event["request_version"]
        if type(version) is not int or version != 1:
            raise ValueError("request_version must be the integer 1")

        ingestion_id = event["ingestion_id"]
        _require_identifier(ingestion_id, "ingestion_id")

        action = event["action"]
        if not isinstance(action, str) or not action:
            raise ValueError("action must be a non-empty string")

        owner = event["ingestion_owner"]
        if not isinstance(owner, dict) or set(owner) != {
            "execution_id",
            "fencing_token",
            "attempt",
        }:
            raise ValueError(
                "ingestion_owner requires execution_id, fencing_token "
                "and attempt"
            )

        execution_id = owner["execution_id"]
        _require_identifier(execution_id, "execution_id")

        fencing_token = owner["fencing_token"]
        attempt = owner["attempt"]
        for field, value in (
            ("fencing_token", fencing_token),
            ("attempt", attempt),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{field} must be a positive integer")

        record = registry._read(f"INGESTION#{ingestion_id}", "OWNERSHIP")
        if record is None:
            raise OwnershipLost(
                f"Ingestion {ingestion_id}: no current ownership claim"
            )

        lease_expires_at = int(record["lease_expires_at"])
        if (
            record["execution_id"] != execution_id
            or int(record["fencing_token"]) != fencing_token
            or int(record["attempt"]) != attempt
            or lease_expires_at <= now()
        ):
            raise OwnershipLost(
                f"Ingestion {ingestion_id}: ownership is expired or superseded"
            )

        claim = OwnershipClaim(
            ingestion_id=ingestion_id,
            execution_id=execution_id,
            fencing_token=fencing_token,
            attempt=attempt,
            lease_expires_at=lease_expires_at,
            recovery_required=record["recovery_required"],
        )

        return handler.handle(
            {
                "request_version": version,
                "action": action,
                "ingestion_id": ingestion_id,
            },
            claim=claim,
        )

    return entry_point


def _require_identifier(value: Any, field: str) -> None:
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