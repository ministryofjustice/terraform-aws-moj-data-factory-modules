from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from uuid import NAMESPACE_DNS, uuid4, uuid5

from botocore.exceptions import ClientError

from ingestion_registry.admission import (
    IngestionRegistry,
    RegistryBusy,
    RequestConflict,
)
from ingestion_registry.replay_requests import ReplayRequest


class ReplayConflict(ValueError):
    """The requested historical input cannot be admitted for replay."""


@dataclass(frozen=True)
class ReplayAdmissionResult:
    replay_id: str
    source_ingestion_id: str
    source_ingestion_version: int
    duplicate: bool


class ReplayRegistry:
    def __init__(self, registry: IngestionRegistry) -> None:
        self.registry = registry

    def _existing_request(
        self,
        request: ReplayRequest,
    ) -> ReplayAdmissionResult | None:
        existing = self.registry._read(
            request.partition_key,
            "META",
        )

        if existing is None:
            return None

        if (
            existing.get("request_fingerprint") != request.fingerprint
            or existing.get("operation") != "REPLAY"
        ):
            raise RequestConflict(
                "The request ID is already registered "
                "with different content"
            )

        return ReplayAdmissionResult(
            replay_id=existing["replay_id"],
            source_ingestion_id=existing["source_ingestion_id"],
            source_ingestion_version=int(
                existing["source_ingestion_version"]
            ),
            duplicate=True,
        )

    def admit(
        self,
        request: ReplayRequest,
    ) -> ReplayAdmissionResult:
        if not isinstance(request, ReplayRequest):
            raise ValueError("request must be a ReplayRequest")

        registry = self.registry
        source_pk = f"INGESTION#{request.source_ingestion_id}"

        for _ in range(3):
            existing = self._existing_request(request)

            if existing is not None:
                return existing

            source = registry._read(source_pk, "META")
            history = registry._read(
                source_pk,
                "STAGE#HISTORY_BASELINE",
            )

            if source is None:
                raise ReplayConflict(
                    "The source ingestion does not exist"
                )

            if source["table_identity"] != asdict(request.table):
                raise ReplayConflict(
                    "The source ingestion belongs to a different table"
                )

            if history is None or history["status"] != "SUCCEEDED":
                raise ReplayConflict(
                    "Replay requires committed baseline history"
                )

            reference = history.get("result_reference")

            if (
                not isinstance(reference, dict)
                or any(
                    reference.get(field) != value
                    for field, value in asdict(
                        request.history_reference
                    ).items()
                )
            ):
                raise ReplayConflict(
                    "The requested history reference does not match "
                    "the source ingestion's committed history"
                )

            identity = request.table
            expected_contract_id = str(
                uuid5(
                    NAMESPACE_DNS,
                    (
                        f"{identity.source}:"
                        f"{identity.database_name}:"
                        f"{identity.schema_name}:"
                        f"{identity.table_name}"
                    ),
                )
            )
            expected_contract_key = (
                f"{identity.source}/{identity.database_name}/"
                f"{identity.schema_name}/{identity.table_name}/"
                f"{request.contract.contract_version}/contract.avsc"
            )

            if (
                request.contract.contract_id != expected_contract_id
                or request.contract.object_reference.object_key
                != expected_contract_key
            ):
                raise ReplayConflict(
                    "The selected contract does not match the replay table"
                )

            replay_id = str(uuid4())
            source_version = int(source["ingestion_version"])
            timestamp = datetime.now(timezone.utc).isoformat()

            request_record = {
                "pk": request.partition_key,
                "sk": "META",
                "record_type": "REQUEST",
                "operation": "REPLAY",
                "request_id": request.request_id,
                "request_fingerprint": request.fingerprint,
                "replay_id": replay_id,
                "source_ingestion_id": request.source_ingestion_id,
                "source_ingestion_version": source_version,
                "created_at": timestamp,
            }

            replay_record = {
                "pk": f"REPLAY#{replay_id}",
                "sk": "META",
                "record_type": "REPLAY",
                "replay_id": replay_id,
                "source_ingestion_id": request.source_ingestion_id,
                "source_ingestion_version": source_version,
                "table_identity": asdict(request.table),
                "source_configuration_revision": source[
                    "configuration_revision"
                ],
                "source_artifact_revision": source["artifact_revision"],
                "history_reference": asdict(request.history_reference),
                "processing_revision": request.processing_revision,
                "contract": asdict(request.contract),
                "request_key": request.partition_key,
                "status": "CREATED",
                "attempt_count": 0,
                "created_at": timestamp,
                "updated_at": timestamp,
            }

            try:
                registry.client.transact_write_items(
                    TransactItems=[
                        registry._put_new(request_record),
                        registry._put_new(replay_record),
                        {
                            "ConditionCheck": {
                                "TableName": registry.table_name,
                                "Key": registry._encode(
                                    {
                                        "pk": source_pk,
                                        "sk": "META",
                                    }
                                ),
                                "ConditionExpression": (
                                    "table_identity = :table "
                                    "AND ingestion_version = :version "
                                    "AND configuration_revision = :config "
                                    "AND artifact_revision = :artifact"
                                ),
                                "ExpressionAttributeValues": registry._encode(
                                    {
                                        ":table": source["table_identity"],
                                        ":version": source_version,
                                        ":config": source[
                                            "configuration_revision"
                                        ],
                                        ":artifact": source[
                                            "artifact_revision"
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
                                        "pk": source_pk,
                                        "sk": "STAGE#HISTORY_BASELINE",
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
                                        ":revision": int(history["revision"]),
                                        ":reference": reference,
                                    }
                                ),
                            }
                        },
                    ],
                    ClientRequestToken=str(uuid4()),
                )

                return ReplayAdmissionResult(
                    replay_id=replay_id,
                    source_ingestion_id=request.source_ingestion_id,
                    source_ingestion_version=source_version,
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
            "Replay admission encountered concurrent changes. "
            "Retry using the same request ID and content."
        )