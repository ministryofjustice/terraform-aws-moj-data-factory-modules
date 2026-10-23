import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from botocore.exceptions import ClientError

from ingestion_registry.admission import IngestionRegistry
from ingestion_registry.identity import (
    TaskGroupIdentity,
    identity_digest,
)
from ingestion_registry.ownership import CLAIMABLE_STATUSES


class TaskGroupConflict(RuntimeError):
    """Another coordinator or incompatible binding owns the task scope."""


class TaskGroupUnavailable(RuntimeError):
    """The task scope cannot currently be claimed."""


@dataclass(frozen=True)
class TaskGroupClaim:
    group_key: str
    task_key: str
    task_arn: str
    execution_id: str
    group_fencing_token: int
    task_fencing_token: int
    scope_fingerprint: str
    lease_expires_at: int
    recovery_required: bool


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


class TaskGroupOwnership:
    def __init__(
        self,
        registry: IngestionRegistry,
        *,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.registry = registry
        self.clock = clock or (lambda: int(time.time()))

    def claim(
        self,
        *,
        ingestion_id: str,
        execution_id: str,
        lease_seconds: int = 300,
    ) -> TaskGroupClaim:
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
            pinned = registry._read(ingestion_pk, "BINDING")

            if (
                ingestion is None
                or ingestion["status"] not in CLAIMABLE_STATUSES
                or pinned is None
            ):
                raise TaskGroupUnavailable(
                    "An active ingestion with pinned resources is required"
                )

            binding = pinned["binding"]
            task_arn = binding["resources"]["replication_task_arn"]

            group_key = TaskGroupIdentity(
                environment=binding["environment"],
                source=binding["source"],
                database_name=binding["database_name"],
                task_group=binding["task_group"]["name"],
            ).partition_key

            task_key = "DMSTASK#" + identity_digest([task_arn])

            tables = sorted(
                [
                    {
                        "schema_name": table["schema_name"],
                        "table_name": table["table_name"],
                    }
                    for table in binding["task_group"]["tables"]
                ],
                key=lambda table: (
                    table["schema_name"],
                    table["table_name"],
                ),
            )

            scope = {
                "environment": binding["environment"],
                "account_id": binding["account_id"],
                "region": binding["region"],
                "source": binding["source"],
                "database_name": binding["database_name"],
                "task_group": binding["task_group"]["name"],
                "configuration_revision": binding["configuration_revision"],
                "artifact_revision": binding["artifact_revision"],
                "extraction_generation": binding["extraction_generation"],
                "task_arn": task_arn,
                "landing": binding["landing"],
                "tables": tables,
            }
            scope_fingerprint = _fingerprint(scope)

            retirement_key = f"RETIRED#{scope_fingerprint}"
            if registry._read(group_key, retirement_key) is not None:
                raise TaskGroupConflict(
                    "This task scope has been permanently retired"
                )

            group_owner = registry._read(group_key, "OWNERSHIP")
            task_owner = registry._read(task_key, "OWNERSHIP")

            for existing, paired_key in (
                (group_owner, task_key),
                (task_owner, group_key),
            ):
                if existing is None:
                    continue

                if existing.get("retired", False):
                    if paired_key == group_key:
                        raise TaskGroupConflict(
                            "A retired physical task cannot be reused"
                        )
                    continue

                if (
                    existing["paired_key"] != paired_key
                    or existing["scope_fingerprint"] != scope_fingerprint
                ):
                    raise TaskGroupConflict(
                        "Task scope differs from its recorded binding. "
                        "Reconcile and explicitly retire the previous "
                        "task scope before changing deployments."
                    )

                if (
                    int(existing["lease_expires_at"]) > now
                    and existing["execution_id"] != execution_id
                ):
                    raise TaskGroupConflict(
                        "Another coordinator owns the task scope"
                    )

            if (
                group_owner is not None
                and task_owner is not None
                and int(group_owner["lease_expires_at"]) > now
                and int(task_owner["lease_expires_at"]) > now
            ):
                return self._result(
                    group_key,
                    task_key,
                    task_arn,
                    group_owner,
                    task_owner,
                )

            expires = now + lease_seconds
            timestamp = datetime.fromtimestamp(
                now,
                tz=timezone.utc,
            ).isoformat()

            recovery_required = (
                group_owner is not None or task_owner is not None
            )

            def lease_record(key, paired_key, previous):
                return {
                    "pk": key,
                    "sk": "OWNERSHIP",
                    "record_type": "TASK_SCOPE_OWNERSHIP",
                    "paired_key": paired_key,
                    "scope_fingerprint": scope_fingerprint,
                    "scope": scope,
                    "execution_id": execution_id,
                    "fencing_token": (
                        int(previous["fencing_token"]) + 1
                        if previous is not None
                        else 1
                    ),
                    "lease_expires_at": expires,
                    "recovery_required": recovery_required,
                    "updated_at": timestamp,
                }

            new_group = lease_record(group_key, task_key, group_owner)
            new_task = lease_record(task_key, group_key, task_owner)

            actions = [
                self._lease_put(new_group, group_owner, now),
                self._lease_put(new_task, task_owner, now),
                {
                    "ConditionCheck": {
                        "TableName": registry.table_name,
                        "Key": registry._encode(
                            {
                                "pk": group_key,
                                "sk": retirement_key,
                            }
                        ),
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
                {
                    "ConditionCheck": {
                        "TableName": registry.table_name,
                        "Key": registry._encode(
                            {"pk": ingestion_pk, "sk": "BINDING"}
                        ),
                        "ConditionExpression": (
                            "binding_fingerprint = :fingerprint"
                        ),
                        "ExpressionAttributeValues": registry._encode(
                            {
                                ":fingerprint": pinned[
                                    "binding_fingerprint"
                                ]
                            }
                        ),
                    }
                },
                {
                    "ConditionCheck": {
                        "TableName": registry.table_name,
                        "Key": registry._encode(
                            {"pk": ingestion_pk, "sk": "META"}
                        ),
                        "ConditionExpression": (
                            "#status IN "
                            "(:created, :running, :failed, :recovering)"
                        ),
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": registry._encode(
                            {
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
                    TransactItems=actions,
                    ClientRequestToken=str(uuid4()),
                )

                return self._result(
                    group_key,
                    task_key,
                    task_arn,
                    new_group,
                    new_task,
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

        raise TaskGroupUnavailable(
            "Task ownership changed concurrently. "
            "Reconcile current ownership before retrying."
        )

    def _lease_put(self, record, previous, now):
        registry = self.registry

        action = {
            "TableName": registry.table_name,
            "Item": registry._encode(record),
            "ConditionExpression": "attribute_not_exists(pk)",
        }

        if previous is not None:
            action["ConditionExpression"] = (
                "fencing_token = :previous "
                "AND (lease_expires_at <= :now "
                "OR execution_id = :execution)"
            )
            action["ExpressionAttributeValues"] = registry._encode(
                {
                    ":previous": int(previous["fencing_token"]),
                    ":now": now,
                    ":execution": record["execution_id"],
                }
            )

        return {"Put": action}

    @staticmethod
    def _result(
        group_key,
        task_key,
        task_arn,
        group_owner,
        task_owner,
    ) -> TaskGroupClaim:
        return TaskGroupClaim(
            group_key=group_key,
            task_key=task_key,
            task_arn=task_arn,
            execution_id=group_owner["execution_id"],
            group_fencing_token=int(group_owner["fencing_token"]),
            task_fencing_token=int(task_owner["fencing_token"]),
            scope_fingerprint=group_owner["scope_fingerprint"],
            lease_expires_at=min(
                int(group_owner["lease_expires_at"]),
                int(task_owner["lease_expires_at"]),
            ),
            recovery_required=(
                group_owner["recovery_required"]
                or task_owner["recovery_required"]
            ),
        )

    def _checked_records(
        self,
        claim: TaskGroupClaim,
        now: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        group = self.registry._read(claim.group_key, "OWNERSHIP")
        task = self.registry._read(claim.task_key, "OWNERSHIP")

        for record, paired_key, token in (
            (group, claim.task_key, claim.group_fencing_token),
            (task, claim.group_key, claim.task_fencing_token),
        ):
            if (
                record is None
                or record["paired_key"] != paired_key
                or record["scope_fingerprint"] != claim.scope_fingerprint
                or record["scope"]["task_arn"] != claim.task_arn
                or record["execution_id"] != claim.execution_id
                or int(record["fencing_token"]) != token
                or int(record["lease_expires_at"]) <= now
            ):
                raise TaskGroupConflict(
                    "The coordinator no longer owns both active task leases"
                )

        return group, task

    def _lease_updates(
        self,
        claim: TaskGroupClaim,
        group: dict[str, Any],
        task: dict[str, Any],
        *,
        now: int,
        expires: int,
    ) -> list[dict[str, Any]]:
        registry = self.registry
        timestamp = datetime.fromtimestamp(
            now,
            tz=timezone.utc,
        ).isoformat()

        actions = []

        for key, paired_key, token, previous in (
            (
                claim.group_key,
                claim.task_key,
                claim.group_fencing_token,
                group,
            ),
            (
                claim.task_key,
                claim.group_key,
                claim.task_fencing_token,
                task,
            ),
        ):
            actions.append(
                {
                    "Update": {
                        "TableName": registry.table_name,
                        "Key": registry._encode(
                            {"pk": key, "sk": "OWNERSHIP"}
                        ),
                        "UpdateExpression": (
                            "SET lease_expires_at = :expires, "
                            "updated_at = :updated"
                        ),
                        "ConditionExpression": (
                            "paired_key = :paired "
                            "AND scope_fingerprint = :scope "
                            "AND execution_id = :execution "
                            "AND fencing_token = :token "
                            "AND lease_expires_at = :previous "
                            "AND lease_expires_at > :now"
                        ),
                        "ExpressionAttributeValues": registry._encode(
                            {
                                ":paired": paired_key,
                                ":scope": claim.scope_fingerprint,
                                ":execution": claim.execution_id,
                                ":token": token,
                                ":previous": int(
                                    previous["lease_expires_at"]
                                ),
                                ":now": now,
                                ":expires": expires,
                                ":updated": timestamp,
                            }
                        ),
                    }
                }
            )

        return actions

    def _now(self) -> int:
        now = self.clock()

        if (
            not isinstance(now, int)
            or isinstance(now, bool)
            or now < 0
        ):
            raise ValueError(
                "Registry clock must return non-negative epoch seconds"
            )

        return now

    def renew(
        self,
        claim: TaskGroupClaim,
        *,
        lease_seconds: int = 300,
    ) -> TaskGroupClaim:
        if (
            not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or not 1 <= lease_seconds <= 3600
        ):
            raise ValueError(
                "lease_seconds must be an integer from 1 to 3600"
            )

        for _ in range(3):
            now = self._now()
            group, task = self._checked_records(claim, now)

            expires = max(
                now + lease_seconds,
                int(group["lease_expires_at"]),
                int(task["lease_expires_at"]),
            )

            try:
                self.registry.client.transact_write_items(
                    TransactItems=self._lease_updates(
                        claim,
                        group,
                        task,
                        now=now,
                        expires=expires,
                    ),
                    ClientRequestToken=str(uuid4()),
                )

                return self._result(
                    claim.group_key,
                    claim.task_key,
                    claim.task_arn,
                    {**group, "lease_expires_at": expires},
                    {**task, "lease_expires_at": expires},
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

        raise TaskGroupUnavailable(
            "Task leases changed during renewal. "
            "Reconcile current ownership before continuing."
        )

    def release(
        self,
        claim: TaskGroupClaim,
        *,
        release_id: str,
        result_reference: dict[str, str],
    ) -> dict[str, Any]:
        """Release coordination ownership and retain an immutable receipt.

        The reference must identify durable evidence of the completed
        coordinator operation. The caller verifies that evidence.

        A release receipt does not assert that DMS has stopped or that
        the coordinator still owns the task.
        """
        if (
            not isinstance(release_id, str)
            or not release_id
            or release_id != release_id.strip()
            or len(release_id.encode("utf-8")) > 256
        ):
            raise ValueError(
                "release_id must contain 1 to 256 UTF-8 bytes "
                "without surrounding whitespace"
            )

        if (
            not isinstance(result_reference, dict)
            or not result_reference
            or not all(
                isinstance(key, str)
                and bool(key.strip())
                and isinstance(value, str)
                and bool(value.strip())
                for key, value in result_reference.items()
            )
        ):
            raise ValueError(
                "result_reference must be a non-empty string map"
            )

        reference = json.loads(json.dumps(result_reference))

        if len(json.dumps(reference).encode("utf-8")) > 8192:
            raise ValueError("result_reference must not exceed 8 KiB")

        payload = {
            "group_key": claim.group_key,
            "task_key": claim.task_key,
            "task_arn": claim.task_arn,
            "execution_id": claim.execution_id,
            "group_fencing_token": claim.group_fencing_token,
            "task_fencing_token": claim.task_fencing_token,
            "scope_fingerprint": claim.scope_fingerprint,
            "result_reference": reference,
        }
        digest = _fingerprint(payload)
        release_key = "RELEASE#" + identity_digest([release_id])

        def recorded_release():
            existing = self.registry._read(
                claim.group_key,
                release_key,
            )

            if existing is not None:
                if existing["release_fingerprint"] != digest:
                    raise TaskGroupConflict(
                        "The release ID is already recorded "
                        "with different content"
                    )

            return existing

        existing = recorded_release()

        if existing is not None:
            return existing

        now = self._now()
        group, task = self._checked_records(claim, now)

        receipt = {
            "pk": claim.group_key,
            "sk": release_key,
            "record_type": "TASK_SCOPE_RELEASE",
            "release_id": release_id,
            "release_fingerprint": digest,
            **payload,
            "released_at": datetime.fromtimestamp(
                now,
                tz=timezone.utc,
            ).isoformat(),
        }

        try:
            self.registry.client.transact_write_items(
                TransactItems=self._lease_updates(
                    claim,
                    group,
                    task,
                    now=now,
                    expires=0,
                )
                + [self.registry._put_new(receipt)],
                ClientRequestToken=str(uuid4()),
            )

        except ClientError as error:
            if (
                error.response["Error"]["Code"]
                != "TransactionCanceledException"
            ):
                raise

            existing = recorded_release()

            if existing is not None:
                return existing

            reasons = error.response.get("CancellationReasons", [])

            if any(
                reason.get("Code") == "ConditionalCheckFailed"
                for reason in reasons
            ):
                raise TaskGroupConflict(
                    "Task ownership changed during release. "
                    "Reconcile current ownership before retrying."
                ) from error

            raise

        return receipt
