from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from boto3.dynamodb.types import (
    TypeDeserializer,
    TypeSerializer,
)
from botocore.exceptions import ClientError

from ingestion_registry.requests import NewIngestionRequest


class RequestConflict(ValueError):
    """The same request ID was used with different content."""


class IngestionConflict(ValueError):
    """The requested table operation conflicts with existing work."""


class RegistryBusy(RuntimeError):
    """Admission could not complete after bounded contention retries."""


@dataclass(frozen=True)
class AdmissionResult:
    ingestion_id: str
    ingestion_version: int
    duplicate: bool


class IngestionRegistry:
    def __init__(
        self,
        client: Any,
        *,
        table_name: str,
    ) -> None:
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValueError("table_name must not be empty")

        self.client = client
        self.table_name = table_name
        self.serializer = TypeSerializer()
        self.deserializer = TypeDeserializer()

    def _encode(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            key: self.serializer.serialize(value)
            for key, value in item.items()
        }

    def _read(
        self,
        partition_key: str,
        sort_key: str,
    ) -> dict[str, Any] | None:
        response = self.client.get_item(
            TableName=self.table_name,
            Key=self._encode(
                {
                    "pk": partition_key,
                    "sk": sort_key,
                }
            ),
            ConsistentRead=True,
        )

        item = response.get("Item")

        if item is None:
            return None

        return {
            key: self.deserializer.deserialize(value)
            for key, value in item.items()
        }

    def _existing_request(
        self,
        request: NewIngestionRequest,
    ) -> AdmissionResult | None:
        existing = self._read(request.partition_key, "META")

        if existing is None:
            return None

        if existing["request_fingerprint"] != request.fingerprint:
            raise RequestConflict(
                "The request ID is already registered "
                "with different content"
            )

        return AdmissionResult(
            ingestion_id=existing["ingestion_id"],
            ingestion_version=int(existing["ingestion_version"]),
            duplicate=True,
        )

    def _put_new(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "Put": {
                "TableName": self.table_name,
                "Item": self._encode(item),
                "ConditionExpression": "attribute_not_exists(pk)",
            }
        }

    def admit(
        self,
        request: NewIngestionRequest,
    ) -> AdmissionResult:
        """Create an ingestion or return its original admission result."""
        for _ in range(3):
            existing = self._existing_request(request)

            if existing is not None:
                return existing

            table_pk = request.table.partition_key
            head = self._read(table_pk, "HEAD")

            conflict = None

            if head is None:
                if request.operation != "INITIAL_INGESTION":
                    conflict = (
                        "Re-ingestion requires an existing table ingestion"
                    )
                previous_version = 0
            else:
                previous_version = int(head["latest_version"])

                if request.operation == "INITIAL_INGESTION":
                    conflict = "Initial ingestion already exists for this table"
                elif head.get("active_ingestion_id"):
                    conflict = (
                        "The table already has an active ingestion; "
                        "recover or finish it before requesting re-ingestion"
                    )

            if conflict is not None:
                existing = self._existing_request(request)

                if existing is not None:
                    return existing

                raise IngestionConflict(conflict)

            version = previous_version + 1
            ingestion_id = str(uuid4())
            ingestion_pk = f"INGESTION#{ingestion_id}"
            now = datetime.now(timezone.utc).isoformat()

            request_record = {
                "pk": request.partition_key,
                "sk": "META",
                "record_type": "REQUEST",
                "request_id": request.request_id,
                "request_fingerprint": request.fingerprint,
                "ingestion_id": ingestion_id,
                "ingestion_version": version,
                "created_at": now,
            }

            ingestion_record = {
                "pk": ingestion_pk,
                "sk": "META",
                "record_type": "INGESTION",
                "ingestion_id": ingestion_id,
                "ingestion_version": version,
                "table_identity": asdict(request.table),
                "operation": request.operation,
                "configuration_revision": request.configuration_revision,
                "artifact_revision": request.artifact_revision,
                "extraction_generation": request.extraction_generation,
                "request_key": request.partition_key,
                "status": "CREATED",
                "attempt_count": 0,
                "created_at": now,
                "updated_at": now,
            }

            version_record = {
                "pk": table_pk,
                "sk": f"VERSION#{version:020d}",
                "record_type": "TABLE_VERSION",
                "ingestion_id": ingestion_id,
                "ingestion_version": version,
                "created_at": now,
            }

            if head is None:
                head_action = self._put_new(
                    {
                        "pk": table_pk,
                        "sk": "HEAD",
                        "record_type": "TABLE_HEAD",
                        "table_identity": asdict(request.table),
                        "latest_version": version,
                        "active_ingestion_id": ingestion_id,
                        "updated_at": now,
                    }
                )
            else:
                head_action = {
                    "Update": {
                        "TableName": self.table_name,
                        "Key": self._encode(
                            {"pk": table_pk, "sk": "HEAD"}
                        ),
                        "UpdateExpression": (
                            "SET latest_version = :next, "
                            "active_ingestion_id = :ingestion, "
                            "updated_at = :now"
                        ),
                        "ConditionExpression": (
                            "latest_version = :previous "
                            "AND attribute_not_exists(active_ingestion_id)"
                        ),
                        "ExpressionAttributeValues": self._encode(
                            {
                                ":next": version,
                                ":previous": previous_version,
                                ":ingestion": ingestion_id,
                                ":now": now,
                            }
                        ),
                    }
                }

            try:
                self.client.transact_write_items(
                    TransactItems=[
                        self._put_new(request_record),
                        head_action,
                        self._put_new(ingestion_record),
                        self._put_new(version_record),
                    ],
                    ClientRequestToken=str(uuid4()),
                )

                return AdmissionResult(
                    ingestion_id=ingestion_id,
                    ingestion_version=version,
                    duplicate=False,
                )

            except ClientError as error:
                if (
                    error.response["Error"]["Code"]
                    != "TransactionCanceledException"
                ):
                    raise

                existing = self._existing_request(request)

                if existing is not None:
                    return existing

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

        raise RegistryBusy(
            "Admission encountered concurrent changes. "
            "Retry using the same request ID and content."
        )