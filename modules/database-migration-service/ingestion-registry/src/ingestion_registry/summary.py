import time
from collections.abc import Callable
from typing import Any

from ingestion_registry.admission import IngestionRegistry
from ingestion_registry.progress import STAGES


class IngestionSummary:
    def __init__(
        self,
        registry: IngestionRegistry,
        *,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.registry = registry
        self.clock = clock or (lambda: int(time.time()))

    def read(self, ingestion_id: str) -> dict[str, Any]:
        if (
            not isinstance(ingestion_id, str)
            or not ingestion_id
            or ingestion_id != ingestion_id.strip()
            or len(ingestion_id.encode("utf-8")) > 256
        ):
            raise ValueError(
                "ingestion_id must contain 1 to 256 UTF-8 bytes"
            )

        keys = ["META", "OWNERSHIP", "BASELINE"] + [
            f"STAGE#{stage}"
            for stage in sorted(STAGES)
        ]

        response = self.registry.client.transact_get_items(
            TransactItems=[
                {
                    "Get": {
                        "TableName": self.registry.table_name,
                        "Key": self.registry._encode(
                            {
                                "pk": f"INGESTION#{ingestion_id}",
                                "sk": key,
                            }
                        ),
                    }
                }
                for key in keys
            ]
        )

        records = {
            key: {
                field: self.registry.deserializer.deserialize(value)
                for field, value in result.get("Item", {}).items()
            }
            for key, result in zip(
                keys,
                response["Responses"],
                strict=True,
            )
        }

        metadata = records["META"]

        if not metadata:
            raise ValueError("Ingestion does not exist")

        now = self.clock()

        if (
            not isinstance(now, int)
            or isinstance(now, bool)
            or now < 0
        ):
            raise ValueError(
                "Registry clock must return non-negative epoch seconds"
            )

        owner = records["OWNERSHIP"]

        ownership_active = bool(
            owner
            and int(owner["lease_expires_at"]) > now
            and owner["execution_id"]
            == metadata.get("active_execution_id")
            and int(owner["attempt"])
            == int(metadata["attempt_count"])
        )

        stages = {
            stage: {
                "status": record["status"],
                "revision": int(record["revision"]),
                "result_reference": record.get("result_reference"),
                "error_summary": record.get("error_summary"),
            }
            for stage in sorted(STAGES)
            if (record := records[f"STAGE#{stage}"])
        }

        failed = [
            name
            for name, stage in stages.items()
            if stage["status"] == "FAILED"
        ]
        blocked = [
            name
            for name, stage in stages.items()
            if stage["status"] == "BLOCKED"
        ]

        if metadata["status"] == "COMPLETED":
            status = "COMPLETED"
            ownership_active = False
        elif failed:
            status = "FAILED"
        elif blocked:
            status = "BLOCKED"
        elif owner and not ownership_active:
            status = "RECOVERY_REQUIRED"
        elif ownership_active:
            status = "RUNNING"
        else:
            status = "CREATED"

        return {
            "ingestion_id": ingestion_id,
            "ingestion_version": int(metadata["ingestion_version"]),
            "table_identity": metadata["table_identity"],
            "configuration_revision": metadata[
                "configuration_revision"
            ],
            "artifact_revision": metadata["artifact_revision"],
            "status": status,
            "metadata_status": metadata["status"],
            "attempt_count": int(metadata["attempt_count"]),
            "ownership_active": ownership_active,
            "recovery_required": bool(
                status != "COMPLETED"
                and owner
                and (
                    not ownership_active
                    or owner.get("recovery_required", False)
                )
            ),
            "baseline_ready": (
                records["BASELINE"].get("status") == "READY"
            ),
            "failed_stages": failed,
            "blocked_stages": blocked,
            "stages": stages,
        }