import copy
import hashlib
import json
import re
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
from ingestion_registry.extraction_policy import (
    resolve_extraction_policy,
)


class BindingConflict(RuntimeError):
    """Resources or a landing boundary conflict with existing records."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(
        _canonical_json(value).encode("utf-8")
    ).hexdigest()


def _reject_float(value: str) -> None:
    raise ValueError("Resource bindings must not contain floating-point values")


class IngestionResourceBindings:
    def __init__(
        self,
        registry: IngestionRegistry,
        *,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.registry = registry
        self.clock = clock or (lambda: int(time.time()))

    def pin(
        self,
        claim: OwnershipClaim,
        resolved_binding: dict[str, Any],
    ) -> dict[str, Any]:
        encoded = _canonical_json(resolved_binding)

        if len(encoded.encode("utf-8")) > 65536:
            raise ValueError("Resolved binding must not exceed 64 KiB")

        snapshot = json.loads(
            encoded,
            parse_float=_reject_float,
        )

        registry = self.registry
        ingestion_pk = f"INGESTION#{claim.ingestion_id}"
        ingestion = registry._read(ingestion_pk, "META")

        if ingestion is None:
            raise BindingConflict("Ingestion does not exist")

        try:
            self._validate(snapshot, ingestion)
        except (KeyError, TypeError, AttributeError) as error:
            raise BindingConflict(
                "Resolved binding is incomplete or has an invalid structure"
            ) from error

        digest = _fingerprint(snapshot)
        landing = snapshot["landing"]
        selected_table = snapshot["table"]

        boundary_key = "BOUNDARY#" + _fingerprint(
            [
                landing["bucket_name"],
                landing["prefix"],
                selected_table["schema_name"],
                selected_table["table_name"],
            ]
        )

        existing = registry._read(ingestion_pk, "BINDING")

        if existing is not None and existing["binding_fingerprint"] != digest:
            raise BindingConflict(
                "The ingestion already has a different pinned binding"
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

        record = {
            "pk": ingestion_pk,
            "sk": "BINDING",
            "record_type": "RESOURCE_BINDING",
            "ingestion_id": claim.ingestion_id,
            "binding_fingerprint": digest,
            "boundary_key": boundary_key,
            "binding": snapshot,
            "created_at": timestamp,
        }

        actions = self._guards(claim, now)

        if existing is None:
            reservation = {
                "pk": boundary_key,
                "sk": "RESERVATION",
                "record_type": "LANDING_RESERVATION",
                "ingestion_id": claim.ingestion_id,
                "table_identity": ingestion["table_identity"],
                "extraction_generation": snapshot[
                    "extraction_generation"
                ],
                "bucket_name": landing["bucket_name"],
                "prefix": landing["prefix"],
                "created_at": timestamp,
            }

            actions.extend(
                [
                    registry._put_new(record),
                    registry._put_new(reservation),
                ]
            )
        else:
            record = existing

            actions.extend(
                [
                    {
                        "ConditionCheck": {
                            "TableName": registry.table_name,
                            "Key": registry._encode(
                                {
                                    "pk": ingestion_pk,
                                    "sk": "BINDING",
                                }
                            ),
                            "ConditionExpression": (
                                "binding_fingerprint = :fingerprint"
                            ),
                            "ExpressionAttributeValues": registry._encode(
                                {":fingerprint": digest}
                            ),
                        }
                    },
                    {
                        "ConditionCheck": {
                            "TableName": registry.table_name,
                            "Key": registry._encode(
                                {
                                    "pk": boundary_key,
                                    "sk": "RESERVATION",
                                }
                            ),
                            "ConditionExpression": (
                                "ingestion_id = :ingestion"
                            ),
                            "ExpressionAttributeValues": registry._encode(
                                {":ingestion": claim.ingestion_id}
                            ),
                        }
                    },
                ]
            )

        try:
            registry.client.transact_write_items(
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
                    "Binding rejected because ownership is no longer valid"
                ) from error

            if any(
                reason.get("Code") == "ConditionalCheckFailed"
                for reason in reasons[2:]
            ):
                raise BindingConflict(
                    "Binding or landing reservation changed concurrently. "
                    "Retry the same binding or reconcile the reservation."
                ) from error

            raise

        return copy.deepcopy(record)

    @staticmethod
    def _validate(
        binding: dict[str, Any],
        ingestion: dict[str, Any],
    ) -> None:
        identity = ingestion["table_identity"]

        for field in ("environment", "source", "database_name"):
            if binding[field] != identity[field]:
                raise BindingConflict(
                    f"Resolved binding {field} does not match the ingestion"
                )

        for field in ("schema_name", "table_name"):
            if binding["table"][field] != identity[field]:
                raise BindingConflict(
                    f"Resolved binding {field} does not match the ingestion"
                )

        for field in (
            "configuration_revision",
            "artifact_revision",
            "extraction_generation",
        ):
            if binding[field] != ingestion[field]:
                raise BindingConflict(
                    f"Resolved binding {field} does not match the ingestion"
                )

        documents = binding["document_references"]

        for name in ("manifest", "artifact"):
            reference = documents[name]

            if (
                not all(
                    isinstance(reference[field], str)
                    and bool(reference[field].strip())
                    for field in (
                        "bucket_name",
                        "object_key",
                        "version_id",
                        "sha256",
                    )
                )
                or reference["version_id"] == "null"
                or re.fullmatch(
                    r"[a-f0-9]{64}",
                    reference["sha256"],
                ) is None
            ):
                raise BindingConflict(
                    f"Invalid pinned {name} document reference"
                )

        landing = binding["landing"]
        prefix = landing["prefix"]

        if (
            not isinstance(landing["bucket_name"], str)
            or not landing["bucket_name"].strip()
            or not isinstance(prefix, str)
            or not prefix.endswith("/")
            or "\\" in prefix
            or any(
                part in ("", ".", "..")
                or part != part.strip()
                for part in prefix[:-1].split("/")
            )
        ):
            raise BindingConflict("Invalid landing boundary")

        group = binding["task_group"]

        try:
            resolve_extraction_policy(binding)
        except ValueError as error:
            raise BindingConflict(str(error)) from error

        if not isinstance(group["name"], str) or not group["name"]:
            raise BindingConflict("Missing task-group identity")

        table_matches = [
            table
            for table in group["tables"]
            if (
                table["schema_name"] == identity["schema_name"]
                and table["table_name"] == identity["table_name"]
            )
        ]

        if len(table_matches) != 1:
            raise BindingConflict(
                "Selected table must occur exactly once in the task group"
            )

        resources = binding["resources"]

        for field in (
            "replication_instance_arn",
            "source_endpoint_arn",
            "target_endpoint_arn",
            "replication_task_arn",
            "replication_task_id",
            "deployment_name",
        ):
            if (
                not isinstance(resources[field], str)
                or not resources[field].strip()
            ):
                raise BindingConflict(
                    f"Missing resource binding: {field}"
                )

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
