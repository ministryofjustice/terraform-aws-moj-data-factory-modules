import copy
import re
import uuid
from dataclasses import asdict
from datetime import datetime, timezone

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from ingestion_registry.identity import TableIdentity, identity_digest


class ContractVersionConflict(ValueError):
    """A version request was reused with different content."""


class ContractVersionBusy(RuntimeError):
    """Version reservation encountered bounded contention."""


class ContractVersions:
    def __init__(self, *, registry, registration_table):
        self.registry = registry
        self.registration_table = registration_table

    @staticmethod
    def _contract_id(table):
        return str(
            uuid.uuid5(
                uuid.NAMESPACE_DNS,
                ":".join(
                    (
                        table.source,
                        table.database_name,
                        table.schema_name,
                        table.table_name,
                    )
                ),
            )
        )

    def _highest_registered_version(self, contract_id):
        request = {
            "KeyConditionExpression": Key("contract_id").eq(contract_id),
            "ConsistentRead": True,
        }
        highest = 0

        while True:
            response = self.registration_table.query(**request)

            for item in response.get("Items", []):
                version = item.get("contract_version")

                if (
                    not isinstance(version, str)
                    or re.fullmatch(r"v[1-9][0-9]*", version) is None
                ):
                    raise ContractVersionConflict(
                        "Existing registration has an unsupported "
                        "contract version; reconcile before allocation"
                    )

                highest = max(highest, int(version[1:]))

            last_key = response.get("LastEvaluatedKey")

            if not last_key:
                return highest

            request["ExclusiveStartKey"] = last_key

    def reserve(
        self,
        *,
        table: TableIdentity,
        request_id: str,
        request_fingerprint: str,
    ) -> dict:
        if not isinstance(table, TableIdentity):
            raise ValueError("table must be a TableIdentity")

        if (
            not isinstance(request_id, str)
            or not request_id
            or request_id != request_id.strip()
            or len(request_id.encode("utf-8")) > 256
        ):
            raise ValueError(
                "request_id must contain 1 to 256 UTF-8 bytes "
                "without surrounding whitespace"
            )

        if (
            not isinstance(request_fingerprint, str)
            or re.fullmatch(
                r"[a-f0-9]{64}", request_fingerprint
            ) is None
        ):
            raise ValueError(
                "request_fingerprint must contain "
                "64 lowercase hexadecimal characters"
            )

        request_pk = "CONTRACTVERSIONREQUEST#" + identity_digest(
            [
                "contract-version-request-v1",
                table.partition_key,
                request_id,
            ]
        )
        contract_id = self._contract_id(table)
        registry = self.registry

        def existing_reservation():
            existing = registry._read(request_pk, "META")

            if existing is None:
                return None

            if (
                existing.get("request_fingerprint")
                != request_fingerprint
                or existing.get("table_identity") != asdict(table)
                or existing.get("contract_id") != contract_id
            ):
                raise ContractVersionConflict(
                    "Version request ID is already recorded "
                    "with different content"
                )

            return copy.deepcopy(existing)

        for _ in range(3):
            existing = existing_reservation()

            if existing is not None:
                return existing

            head = registry._read(
                table.partition_key,
                "CONTRACT_VERSION_HEAD",
            )
            previous = 0 if head is None else int(head["latest_version"])

            registered = self._highest_registered_version(contract_id)
            version = max(previous, registered) + 1
            now = datetime.now(timezone.utc).isoformat()

            reservation = {
                "pk": request_pk,
                "sk": "META",
                "record_type": "CONTRACT_VERSION_RESERVATION",
                "table_identity": asdict(table),
                "request_id": request_id,
                "request_fingerprint": request_fingerprint,
                "contract_id": contract_id,
                "contract_version": f"v{version}",
                "created_at": now,
            }

            next_head = {
                "pk": table.partition_key,
                "sk": "CONTRACT_VERSION_HEAD",
                "record_type": "CONTRACT_VERSION_HEAD",
                "table_identity": asdict(table),
                "contract_id": contract_id,
                "latest_version": version,
                "updated_at": now,
            }

            head_put = {
                "TableName": registry.table_name,
                "Item": registry._encode(next_head),
                "ConditionExpression": "attribute_not_exists(pk)",
            }

            if head is not None:
                if (
                    head.get("table_identity") != asdict(table)
                    or head.get("contract_id") != contract_id
                    or previous < 1
                ):
                    raise ContractVersionConflict(
                        "Contract version head is inconsistent"
                    )

                head_put.update(
                    {
                        "ConditionExpression": (
                            "latest_version = :previous"
                        ),
                        "ExpressionAttributeValues": registry._encode(
                            {":previous": previous}
                        ),
                    }
                )

            try:
                registry.client.transact_write_items(
                    TransactItems=[
                        registry._put_new(reservation),
                        {"Put": head_put},
                    ]
                )
                return copy.deepcopy(reservation)

            except ClientError as error:
                if (
                    error.response["Error"]["Code"]
                    != "TransactionCanceledException"
                ):
                    raise

                existing = existing_reservation()

                if existing is not None:
                    return existing

                reasons = {
                    reason.get("Code")
                    for reason in error.response.get(
                        "CancellationReasons", []
                    )
                }

                if not reasons or not reasons <= {
                    None,
                    "None",
                    "ConditionalCheckFailed",
                    "TransactionConflict",
                }:
                    raise

        raise ContractVersionBusy(
            "Version allocation encountered concurrent changes; "
            "retry the same request and fingerprint"
        )