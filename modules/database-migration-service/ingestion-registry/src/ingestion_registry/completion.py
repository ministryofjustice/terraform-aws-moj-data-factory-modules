import copy
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from ingestion_registry.extraction_policy import (
    resolve_extraction_policy,
)
from ingestion_registry.identity import TableIdentity
from ingestion_registry.ownership import OwnershipClaim
from ingestion_registry.progress import (
    IngestionProgress,
    ProgressConflict,
)
from ingestion_registry.references import S3ObjectReference


CDC_COMPLETION_STAGES = (
    "SOURCE_STOPPED",
    "CDC_DRAINED",
    "HISTORY_FINALIZED",
)


class CompletionConflict(ProgressConflict):
    """Completion conflicts with the ingestion's recorded state."""


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


class IngestionCompletion:
    def __init__(
        self,
        progress: IngestionProgress,
        *,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.progress = progress
        self.registry = progress.registry
        self.clock = clock or (lambda: int(time.time()))

    def complete_full_load(
        self,
        claim: OwnershipClaim,
        *,
        source_stopped_reference: S3ObjectReference,
    ) -> dict[str, Any]:
        if not isinstance(
            source_stopped_reference,
            S3ObjectReference,
        ):
            raise ValueError(
                "source_stopped_reference must be an S3ObjectReference"
            )

        return self._complete(
            claim,
            migration_type="full-load",
            payload={
                "ingestion_id": claim.ingestion_id,
                "completion_type": "FULL_LOAD",
                "source_stopped_reference": asdict(
                    source_stopped_reference
                ),
            },
            shutdown_records={},
        )

    def complete_full_load_and_cdc(
        self,
        claim: OwnershipClaim,
    ) -> dict[str, Any]:
        records = {}

        for stage in CDC_COMPLETION_STAGES:
            record = self.progress.read_stage(
                ingestion_id=claim.ingestion_id,
                stage=stage,
            )

            if record is None or record["status"] != "SUCCEEDED":
                raise CompletionConflict(
                    f"CDC completion requires successful {stage}"
                )

            records[stage] = record

        references = {
            stage: record["result_reference"]
            for stage, record in records.items()
        }

        source = references["SOURCE_STOPPED"]

        if (
            not isinstance(source, dict)
            or not isinstance(source.get("boundary_id"), str)
            or not source["boundary_id"].strip()
            or not isinstance(source.get("task_arn"), str)
            or not source["task_arn"].strip()
        ):
            raise CompletionConflict(
                "Source shutdown evidence has no valid boundary or task"
            )

        for stage, reference in references.items():
            if (
                not isinstance(reference, dict)
                or reference.get("kind") != "cdc-shutdown-evidence"
                or reference.get("stage") != stage
                or reference.get("ingestion_id") != claim.ingestion_id
                or reference.get("task_arn") != source["task_arn"]
                or reference.get("boundary_id") != source["boundary_id"]
            ):
                raise CompletionConflict(
                    "CDC shutdown evidence must match the ingestion, "
                    "task and final input boundary"
                )

            try:
                S3ObjectReference(
                    bucket_name=reference["bucket_name"],
                    object_key=reference["object_key"],
                    version_id=reference["version_id"],
                    sha256=reference["sha256"],
                )
            except (KeyError, TypeError, ValueError) as error:
                raise CompletionConflict(
                    f"{stage} has an invalid evidence reference"
                ) from error

        return self._complete(
            claim,
            migration_type="full-load-and-cdc",
            payload={
                "ingestion_id": claim.ingestion_id,
                "completion_type": "FULL_LOAD_AND_CDC",
                "shutdown_evidence": copy.deepcopy(references),
            },
            shutdown_records=records,
        )

    def _complete(
        self,
        claim: OwnershipClaim,
        *,
        migration_type: str,
        payload: dict[str, Any],
        shutdown_records: dict[str, Any],
    ) -> dict[str, Any]:
        registry = self.registry
        partition_key = f"INGESTION#{claim.ingestion_id}"
        fingerprint = _fingerprint(payload)

        existing = registry._read(partition_key, "COMPLETION")

        if existing is not None:
            if existing["completion_fingerprint"] != fingerprint:
                raise CompletionConflict(
                    "The ingestion already has a different "
                    "completion decision"
                )

            return copy.deepcopy(existing)

        ingestion = registry._read(partition_key, "META")
        binding_record = registry._read(partition_key, "BINDING")
        baseline = registry._read(partition_key, "BASELINE")

        if ingestion is None:
            raise CompletionConflict("Ingestion does not exist")

        if binding_record is None:
            raise CompletionConflict(
                "Completion requires a pinned resource binding"
            )

        binding = binding_record["binding"]
        policy = resolve_extraction_policy(binding)

        if policy.migration_type != migration_type:
            raise CompletionConflict(
                f"Completion requires a {migration_type} binding; "
                "CDC and Full Load use different completion paths"
            )

        if baseline is None or baseline.get("status") != "READY":
            raise CompletionConflict(
                "Completion requires a ready baseline"
            )

        if shutdown_records:
            task_arn = binding["resources"]["replication_task_arn"]

            if any(
                record["result_reference"]["task_arn"] != task_arn
                for record in shutdown_records.values()
            ):
                raise CompletionConflict(
                    "CDC shutdown evidence does not match "
                    "the pinned task"
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

        timestamp = datetime.fromtimestamp(
            now,
            tz=timezone.utc,
        ).isoformat()

        table = TableIdentity(**ingestion["table_identity"])
        version = int(ingestion["ingestion_version"])

        receipt = {
            "pk": partition_key,
            "sk": "COMPLETION",
            "record_type": "INGESTION_COMPLETION",
            **payload,
            "completion_fingerprint": fingerprint,
            "ingestion_version": version,
            "binding_fingerprint": binding_record[
                "binding_fingerprint"
            ],
            "baseline_completed_at": baseline["completed_at"],
            "execution_id": claim.execution_id,
            "attempt": claim.attempt,
            "fencing_token": claim.fencing_token,
            "completed_at": timestamp,
        }

        guards = self.progress._guards(claim, now)

        metadata_update = dict(guards[1]["ConditionCheck"])
        metadata_update["UpdateExpression"] = (
            "SET #status = :completed, "
            "completed_at = :completed_at, "
            "updated_at = :completed_at"
        )
        metadata_update["ExpressionAttributeValues"] = {
            **metadata_update["ExpressionAttributeValues"],
            **registry._encode(
                {
                    ":completed": "COMPLETED",
                    ":completed_at": timestamp,
                }
            ),
        }

        actions = [
            guards[0],
            {"Update": metadata_update},
            {
                "Update": {
                    "TableName": registry.table_name,
                    "Key": registry._encode(
                        {
                            "pk": table.partition_key,
                            "sk": "HEAD",
                        }
                    ),
                    "UpdateExpression": (
                        "SET updated_at = :updated "
                        "REMOVE active_ingestion_id"
                    ),
                    "ConditionExpression": (
                        "active_ingestion_id = :ingestion "
                        "AND latest_version = :version"
                    ),
                    "ExpressionAttributeValues": registry._encode(
                        {
                            ":updated": timestamp,
                            ":ingestion": claim.ingestion_id,
                            ":version": version,
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
                            "sk": "BINDING",
                        }
                    ),
                    "ConditionExpression": (
                        "binding_fingerprint = :fingerprint"
                    ),
                    "ExpressionAttributeValues": registry._encode(
                        {
                            ":fingerprint": binding_record[
                                "binding_fingerprint"
                            ],
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
                            "sk": "BASELINE",
                        }
                    ),
                    "ConditionExpression": (
                        "#status = :ready "
                        "AND completed_at = :completed_at"
                    ),
                    "ExpressionAttributeNames": {
                        "#status": "status",
                    },
                    "ExpressionAttributeValues": registry._encode(
                        {
                            ":ready": "READY",
                            ":completed_at": baseline["completed_at"],
                        }
                    ),
                }
            },
        ]

        for stage, record in shutdown_records.items():
            actions.append(
                {
                    "ConditionCheck": {
                        "TableName": registry.table_name,
                        "Key": registry._encode(
                            {
                                "pk": partition_key,
                                "sk": f"STAGE#{stage}",
                            }
                        ),
                        "ConditionExpression": (
                            "#status = :succeeded "
                            "AND #revision = :revision "
                            "AND result_reference = :reference"
                        ),
                        "ExpressionAttributeNames": {
                            "#status": "status",
                            "#revision": "revision",
                        },
                        "ExpressionAttributeValues": registry._encode(
                            {
                                ":succeeded": "SUCCEEDED",
                                ":revision": int(record["revision"]),
                                ":reference": record["result_reference"],
                            }
                        ),
                    }
                }
            )

        actions.append(registry._put_new(receipt))
        self.progress._commit(actions)

        return copy.deepcopy(receipt)