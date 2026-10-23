import copy
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from botocore.exceptions import ClientError

from ingestion_registry.ownership import OwnershipClaim
from ingestion_registry.progress import (
    IngestionProgress,
    ProgressConflict,
)


BASELINE_STAGES = (
    "CONTRACT",
    "SOURCE_PREFLIGHT",
    "EXTRACTION",
    "PROCESSING",
    "HISTORY_BASELINE",
    "PUBLICATION",
)


class LifecycleConflict(ProgressConflict):
    """The lifecycle decision conflicts with recorded progress."""


class IngestionLifecycle:
    def __init__(
        self,
        progress: IngestionProgress,
        *,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.progress = progress
        self.registry = progress.registry
        self.clock = clock or (lambda: int(time.time()))

    def mark_baseline_ready(
        self,
        claim: OwnershipClaim,
        *,
        reconciliation_required: bool,
    ) -> dict[str, Any]:
        if not isinstance(reconciliation_required, bool):
            raise ValueError(
                "reconciliation_required must be a boolean"
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

        partition_key = f"INGESTION#{claim.ingestion_id}"
        existing = self.registry._read(
            partition_key,
            "BASELINE",
        )

        if existing is not None:
            if (
                existing["reconciliation_required"]
                != reconciliation_required
            ):
                raise LifecycleConflict(
                    "Baseline readiness is already recorded "
                    "with a different reconciliation requirement"
                )

            self.progress._commit(
                self.progress._guards(claim, now)
            )
            return copy.deepcopy(existing)

        required_stages = list(BASELINE_STAGES)

        if reconciliation_required:
            required_stages.append("RECONCILIATION")

        stage_results: dict[str, Any] = {}
        stage_checks: list[dict[str, Any]] = []

        for stage in required_stages:
            record = self.progress.read_stage(
                ingestion_id=claim.ingestion_id,
                stage=stage,
            )

            if record is None or record["status"] != "SUCCEEDED":
                raise LifecycleConflict(
                    f"Baseline is not ready: {stage} "
                    "has not succeeded"
                )

            if not record.get("result_reference"):
                raise LifecycleConflict(
                    f"Baseline is not ready: {stage} "
                    "has no durable result reference"
                )

            revision = int(record["revision"])

            stage_results[stage] = {
                "revision": revision,
                "result_reference": copy.deepcopy(
                    record["result_reference"]
                ),
            }

            stage_checks.append(
                {
                    "ConditionCheck": {
                        "TableName": self.registry.table_name,
                        "Key": self.registry._encode(
                            {
                                "pk": partition_key,
                                "sk": f"STAGE#{stage}",
                            }
                        ),
                        "ConditionExpression": (
                            "#status = :succeeded "
                            "AND #revision = :revision"
                        ),
                        "ExpressionAttributeNames": {
                            "#status": "status",
                            "#revision": "revision",
                        },
                        "ExpressionAttributeValues": (
                            self.registry._encode(
                                {
                                    ":succeeded": "SUCCEEDED",
                                    ":revision": revision,
                                }
                            )
                        ),
                    }
                }
            )

        baseline = {
            "pk": partition_key,
            "sk": "BASELINE",
            "record_type": "BASELINE_READINESS",
            "ingestion_id": claim.ingestion_id,
            "status": "READY",
            "reconciliation_required": reconciliation_required,
            "stage_results": stage_results,
            "execution_id": claim.execution_id,
            "attempt": claim.attempt,
            "fencing_token": claim.fencing_token,
            "completed_at": datetime.fromtimestamp(
                now,
                tz=timezone.utc,
            ).isoformat(),
        }

        try:
            self.progress._commit(
                self.progress._guards(claim, now)
                + stage_checks
                + [self.registry._put_new(baseline)]
            )
        except ProgressConflict as error:
            raise LifecycleConflict(
                "Baseline progress changed concurrently; "
                "retry after reconciling the recorded result"
            ) from error
        except ClientError:
            raise

        return copy.deepcopy(baseline)