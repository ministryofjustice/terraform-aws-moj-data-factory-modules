import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from botocore.exceptions import ClientError

from ingestion_registry.admission import IngestionRegistry
from ingestion_registry.identity import (
    TableIdentity,
    TaskGroupIdentity,
    identity_digest,
    validate_database_identifier,
    validate_slug,
)
from ingestion_registry.ownership import OwnershipClaim
from ingestion_registry.task_groups import TaskGroupClaim

from dms_runtime.requests import OWNED_ACTIONS


class RuntimeScopeDenied(PermissionError):
    """The binding is outside the trusted runtime context."""


class RuntimeOwnershipLost(RuntimeError):
    """Registry ownership no longer permits this operation."""


@dataclass(frozen=True)
class RuntimeContext:
    environment: str
    account_id: str
    region: str
    source: str
    database_name: str

    def __post_init__(self) -> None:
        validate_slug(self.environment, "environment")
        validate_slug(self.source, "source")
        validate_database_identifier(
            self.database_name,
            "database_name",
        )

        if (
            not isinstance(self.account_id, str)
            or re.fullmatch(
                r"[0-9]{12}",
                self.account_id,
            ) is None
        ):
            raise ValueError(
                "account_id must contain 12 digits"
            )

        if (
            not isinstance(self.region, str)
            or not self.region
            or self.region != self.region.strip()
        ):
            raise ValueError(
                "region must be a non-empty trimmed string"
            )


class RegistryScopeGuard:
    def __init__(
        self,
        registry: IngestionRegistry,
        *,
        context: RuntimeContext,
        ingestion_claim: OwnershipClaim,
        task_claim: TaskGroupClaim,
        clock: Callable[[], int] | None = None,
    ) -> None:
        if not isinstance(context, RuntimeContext):
            raise ValueError(
                "context must be a RuntimeContext"
            )

        if (
            not isinstance(ingestion_claim, OwnershipClaim)
            or not isinstance(task_claim, TaskGroupClaim)
        ):
            raise ValueError(
                "Registry ownership claims are required"
            )

        self.registry = registry
        self.context = context
        self.ingestion_claim = ingestion_claim
        self.task_claim = task_claim
        self.clock = clock or (lambda: int(time.time()))

    def _check(
        self,
        pk: str,
        sk: str,
        condition: str,
        values: dict[str, Any] | None = None,
        names: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        check = {
            "TableName": self.registry.table_name,
            "Key": self.registry._encode(
                {
                    "pk": pk,
                    "sk": sk,
                }
            ),
            "ConditionExpression": condition,
        }

        if values is not None:
            check["ExpressionAttributeValues"] = (
                self.registry._encode(values)
            )

        if names is not None:
            check["ExpressionAttributeNames"] = names

        return {"ConditionCheck": check}

    def transaction_checks(
        self,
        binding: dict[str, Any],
        action: str,
    ) -> list[dict[str, Any]]:
        if action not in OWNED_ACTIONS:
            raise ValueError(
                "Unsupported task-scope operation"
            )

        if not isinstance(binding, dict):
            raise RuntimeScopeDenied(
                "A resolved binding object is required"
            )

        for field, expected in asdict(self.context).items():
            if binding.get(field) != expected:
                raise RuntimeScopeDenied(
                    f"Binding {field} is outside "
                    "the authorised runtime context"
                )

        try:
            resources = binding["resources"]
            group = binding["task_group"]

            table = TableIdentity(
                environment=binding["environment"],
                source=binding["source"],
                database_name=binding["database_name"],
                schema_name=binding["table"]["schema_name"],
                table_name=binding["table"]["table_name"],
            )

            group_key = TaskGroupIdentity(
                environment=binding["environment"],
                source=binding["source"],
                database_name=binding["database_name"],
                task_group=group["name"],
            ).partition_key

            task_key = "DMSTASK#" + identity_digest(
                [resources["replication_task_arn"]]
            )

            scope = {
                **asdict(self.context),
                "task_group": group["name"],
                "configuration_revision": binding[
                    "configuration_revision"
                ],
                "artifact_revision": binding["artifact_revision"],
                "extraction_generation": binding[
                    "extraction_generation"
                ],
                "task_arn": resources["replication_task_arn"],
                "landing": binding["landing"],
                "tables": sorted(
                    [
                        {
                            "schema_name": value["schema_name"],
                            "table_name": value["table_name"],
                        }
                        for value in group["tables"]
                    ],
                    key=lambda value: (
                        value["schema_name"],
                        value["table_name"],
                    ),
                ),
            }

            encoded = json.dumps(
                binding,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")

        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeScopeDenied(
                "A complete resolved binding is required"
            ) from error

        if len(encoded) > 65536:
            raise RuntimeScopeDenied(
                "Resolved binding must not exceed 64 KiB"
            )

        digest = hashlib.sha256(encoded).hexdigest()
        claim = self.task_claim

        if (
            claim.group_key,
            claim.task_key,
            claim.task_arn,
        ) != (
            group_key,
            task_key,
            resources["replication_task_arn"],
        ):
            raise RuntimeScopeDenied(
                "Task ownership claim does not match "
                "the resolved scope"
            )

        for field, kind in (
            ("replication_task_arn", "task"),
            ("replication_instance_arn", "rep"),
            ("source_endpoint_arn", "endpoint"),
            ("target_endpoint_arn", "endpoint"),
        ):
            prefix = (
                f"arn:aws:dms:{self.context.region}:"
                f"{self.context.account_id}:{kind}:"
            )
            arn = resources.get(field)

            if (
                not isinstance(arn, str)
                or not arn.startswith(prefix)
                or not arn[len(prefix):]
            ):
                raise RuntimeScopeDenied(
                    "DMS resource is outside the authorised "
                    "account, region or resource type"
                )

        now = self.clock()

        if (
            not isinstance(now, int)
            or isinstance(now, bool)
            or now < 0
        ):
            raise ValueError(
                "Registry clock must return "
                "non-negative epoch seconds"
            )

        ingestion = self.ingestion_claim
        pk = f"INGESTION#{ingestion.ingestion_id}"

        checks = [
            self._check(
                pk,
                "OWNERSHIP",
                (
                    "execution_id = :execution "
                    "AND fencing_token = :token "
                    "AND attempt = :attempt "
                    "AND lease_expires_at > :now"
                ),
                {
                    ":execution": ingestion.execution_id,
                    ":token": ingestion.fencing_token,
                    ":attempt": ingestion.attempt,
                    ":now": now,
                },
            ),
            self._check(
                pk,
                "META",
                (
                    "active_execution_id = :execution "
                    "AND attempt_count = :attempt "
                    "AND #status IN "
                    "(:created, :running, :failed, :recovering) "
                    "AND table_identity = :table "
                    "AND configuration_revision = :config "
                    "AND artifact_revision = :artifact "
                    "AND extraction_generation = :generation"
                ),
                {
                    ":execution": ingestion.execution_id,
                    ":attempt": ingestion.attempt,
                    ":created": "CREATED",
                    ":running": "RUNNING",
                    ":failed": "FAILED",
                    ":recovering": "RECOVERING",
                    ":table": asdict(table),
                    ":config": binding["configuration_revision"],
                    ":artifact": binding["artifact_revision"],
                    ":generation": binding["extraction_generation"],
                },
                {"#status": "status"},
            ),
            self._check(
                pk,
                "BINDING",
                "binding_fingerprint = :digest",
                {":digest": digest},
            ),
            self._check(
                table.partition_key,
                "HEAD",
                "active_ingestion_id = :ingestion",
                {":ingestion": ingestion.ingestion_id},
            ),
            self._check(
                group_key,
                f"RETIRED#{claim.scope_fingerprint}",
                "attribute_not_exists(pk)",
            ),
        ]

        for key, paired, token in (
            (
                group_key,
                task_key,
                claim.group_fencing_token,
            ),
            (
                task_key,
                group_key,
                claim.task_fencing_token,
            ),
        ):
            checks.append(
                self._check(
                    key,
                    "OWNERSHIP",
                    (
                        "paired_key = :paired "
                        "AND scope_fingerprint = :fingerprint "
                        "AND #scope = :scope "
                        "AND execution_id = :execution "
                        "AND fencing_token = :token "
                        "AND lease_expires_at > :now "
                        "AND (attribute_not_exists(#retired) "
                        "OR #retired = :false)"
                    ),
                    {
                        ":paired": paired,
                        ":fingerprint": claim.scope_fingerprint,
                        ":scope": scope,
                        ":execution": claim.execution_id,
                        ":token": token,
                        ":now": now,
                        ":false": False,
                    },
                    {
                        "#scope": "scope",
                        "#retired": "retired",
                    },
                )
            )

        return checks

    def __call__(
        self,
        binding: dict[str, Any],
        action: str,
    ) -> None:
        checks = self.transaction_checks(binding, action)

        try:
            self.registry.client.transact_write_items(
                TransactItems=checks,
            )

        except ClientError as error:
            if (
                error.response["Error"]["Code"]
                == "TransactionCanceledException"
                and any(
                    value.get("Code") == "ConditionalCheckFailed"
                    for value in error.response.get(
                        "CancellationReasons",
                        [],
                    )
                )
            ):
                raise RuntimeOwnershipLost(
                    "Task operation rejected: ownership, binding "
                    "or table reservation changed"
                ) from error

            raise