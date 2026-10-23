from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from botocore.exceptions import ClientError

from ingestion_registry.identity import TableIdentity
from ingestion_registry.references import S3ObjectReference
from ingestion_registry.task_groups import (
    TaskGroupClaim,
    TaskGroupConflict,
    TaskGroupOwnership,
    _fingerprint,
)


MAX_ATOMIC_RETIREMENT_TABLES = 97


class TaskScopeRetirement:
    def __init__(
        self,
        ownership: TaskGroupOwnership,
    ) -> None:
        self.ownership = ownership
        self.registry = ownership.registry

    def retire(
        self,
        claim: TaskGroupClaim,
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

        registry = self.registry
        key = f"RETIRED#{claim.scope_fingerprint}"

        payload = {
            "claim": asdict(claim),
            "source_stopped_reference": asdict(
                source_stopped_reference
            ),
        }
        digest = _fingerprint(payload)

        def previous_receipt():
            previous = registry._read(
                claim.group_key,
                key,
            )

            if (
                previous is not None
                and previous["retirement_fingerprint"] != digest
            ):
                raise TaskGroupConflict(
                    "The scope has a different retirement decision"
                )

            return previous

        previous = previous_receipt()

        if previous is not None:
            return previous

        now = self.ownership._now()
        group = registry._read(claim.group_key, "OWNERSHIP")
        task = registry._read(claim.task_key, "OWNERSHIP")

        for record, paired_key, token in (
            (group, claim.task_key, claim.group_fencing_token),
            (task, claim.group_key, claim.task_fencing_token),
        ):
            if (
                record is None
                or record["paired_key"] != paired_key
                or record["scope_fingerprint"]
                != claim.scope_fingerprint
                or record["scope"]["task_arn"] != claim.task_arn
                or record["execution_id"] != claim.execution_id
                or int(record["fencing_token"]) != token
                or record.get("retired", False)
            ):
                raise TaskGroupConflict(
                    "Coordinator tokens no longer match the scope"
                )

        scope = group["scope"]

        table_keys = {
            TableIdentity(
                environment=scope["environment"],
                source=scope["source"],
                database_name=scope["database_name"],
                schema_name=table["schema_name"],
                table_name=table["table_name"],
            ).partition_key
            for table in scope["tables"]
        }

        if len(table_keys) > MAX_ATOMIC_RETIREMENT_TABLES:
            raise ValueError(
                "Atomic retirement supports at most 97 selected tables; "
                "larger scopes require a separate staged retirement "
                "implementation"
            )

        updates = self.ownership._lease_updates(
            claim,
            group,
            task,
            now=now,
            expires=0,
        )

        for action in updates:
            update = action["Update"]

            update["ConditionExpression"] = (
                update["ConditionExpression"].replace(
                    " AND lease_expires_at > :now",
                    "",
                )
            )
            update["ExpressionAttributeValues"].pop(":now")
            update["UpdateExpression"] += ", retired = :retired"
            update["ExpressionAttributeValues"].update(
                registry._encode({":retired": True})
            )

        receipt = {
            "pk": claim.group_key,
            "sk": key,
            "record_type": "TASK_SCOPE_RETIREMENT",
            "retirement_fingerprint": digest,
            **payload,
            "scope": scope,
            "retired_at": datetime.fromtimestamp(
                now,
                tz=timezone.utc,
            ).isoformat(),
        }

        actions = updates + [registry._put_new(receipt)]

        for table_key in sorted(table_keys):
            actions.append(
                {
                    "ConditionCheck": {
                        "TableName": registry.table_name,
                        "Key": registry._encode(
                            {
                                "pk": table_key,
                                "sk": "HEAD",
                            }
                        ),
                        "ConditionExpression": (
                            "attribute_not_exists(active_ingestion_id)"
                        ),
                    }
                }
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

            previous = previous_receipt()

            if previous is not None:
                return previous

            if any(
                reason.get("Code") == "ConditionalCheckFailed"
                for reason in error.response.get(
                    "CancellationReasons",
                    [],
                )
            ):
                raise TaskGroupConflict(
                    "Retirement requires current coordinator ownership "
                    "and no active ingestion for any selected table"
                ) from error

            raise

        return receipt