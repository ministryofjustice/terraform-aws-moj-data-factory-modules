import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from dms_runtime.batch_processing import ProcessingCompletion
from dms_runtime.downstream_commit_adapter import DownstreamCommitConflict
from dms_runtime.processing_result_store import MAX_RESULT_BYTES
from ingestion_registry.references import S3ObjectReference


@dataclass(frozen=True)
class CatalogueRegistration:
    database: str
    table: str
    ingestion_id: str
    operation_id: str
    request_fingerprint: str
    history_location: str
    history_commit: S3ObjectReference
    contract: S3ObjectReference


class CatalogueEntryExists(RuntimeError):
    """The catalogue name is already registered."""


class PublicationCatalogue(Protocol):
    def create(self, registration: CatalogueRegistration) -> None:
        """Create a registration or raise CatalogueEntryExists."""

    def read(
        self, *, database: str, table: str
    ) -> CatalogueRegistration | None:
        """Read the durable registration for this name if present."""


class PublicationTarget:
    def __init__(
        self,
        *,
        client: Any,
        catalogue: PublicationCatalogue,
        history_bucket: str,
        history_bucket_owner: str,
        history_prefix: str,
        database: str,
        table: str,
    ) -> None:
        if not history_prefix or not history_prefix.endswith("/"):
            raise ValueError("history_prefix must end with '/'")
        self._client = client
        self._catalogue = catalogue
        self._bucket = history_bucket
        self._owner = history_bucket_owner
        self._prefix = history_prefix
        self._database = database
        self._table = table

    def commit(
        self,
        *,
        operation_id: str,
        request_fingerprint: str,
        request: Mapping[str, Any],
    ) -> ProcessingCompletion | None:
        expected = self._registration(
            operation_id, request_fingerprint, request
        )
        try:
            self._catalogue.create(expected)
        except CatalogueEntryExists:
            pass
        return self._read_completion(expected)

    def lookup(
        self,
        *,
        operation_id: str,
        request_fingerprint: str,
        request: Mapping[str, Any],
    ) -> ProcessingCompletion | None:
        expected = self._registration(
            operation_id, request_fingerprint, request
        )
        return self._read_completion(expected)

    def _registration(
        self,
        operation_id: str,
        request_fingerprint: str,
        request: Mapping[str, Any],
    ) -> CatalogueRegistration:
        try:
            history = S3ObjectReference(**request["history_commit"])
            pinned_contract = request["contract"]
            if pinned_contract["kind"] != "registered-contract":
                raise ValueError("Expected a registered contract")
            contract = S3ObjectReference(
                bucket_name=pinned_contract["contract_bucket_name"],
                object_key=pinned_contract["contract_object_key"],
                version_id=pinned_contract["contract_object_version_id"],
                sha256=pinned_contract["contract_object_sha256"],
            )
            ingestion_id = request["ingestion_id"]
            history_location = request["history_location"]
            table = request["deployment"]["table"]
            expected_table = {
                "schema_name": table["schema_name"],
                "table_name": table["table_name"],
            }
            history_operation_id = hashlib.sha256(
                json.dumps(
                    {"ingestion_id": ingestion_id, "stage": "HISTORY_BASELINE"},
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
        except (KeyError, TypeError, ValueError) as error:
            raise DownstreamCommitConflict(
                "Publication requires pinned History and contract references"
            ) from error

        commit_prefix = f"{self._prefix}commits/{history_operation_id}/"
        expected_location = (
            f"s3://{self._bucket}/{self._prefix}data/{history_operation_id}/"
        )
        if (
            history.bucket_name != self._bucket
            or not history.object_key.startswith(commit_prefix)
            or history_location != expected_location
            or request.get("history_commit_id") != history.sha256
        ):
            raise DownstreamCommitConflict(
                "History commit is outside the configured publication boundary"
            )

        document = self._read_history_commit(history)
        output = document.get("output")
        if (
            type(document.get("evidence_version")) is not int
            or document["evidence_version"] != 1
            or document.get("operation_id") != history_operation_id
            or document.get("ingestion_id") != ingestion_id
            or document.get("table") != expected_table
            or document.get("contract") != pinned_contract
            or not isinstance(output, dict)
            or output.get("history_location") != history_location
            or output.get("contract") != asdict(contract)
            or not isinstance(output.get("objects"), list)
        ):
            raise DownstreamCommitConflict(
                "History commit does not match the pinned Publication request"
            )

        return CatalogueRegistration(
            database=self._database,
            table=self._table,
            ingestion_id=ingestion_id,
            operation_id=operation_id,
            request_fingerprint=request_fingerprint,
            history_location=history_location,
            history_commit=history,
            contract=contract,
        )

    def _read_history_commit(
        self, reference: S3ObjectReference
    ) -> dict[str, Any]:
        response = self._client.get_object(
            Bucket=reference.bucket_name,
            Key=reference.object_key,
            VersionId=reference.version_id,
            ExpectedBucketOwner=self._owner,
        )
        body = response["Body"]
        try:
            if response.get("VersionId") != reference.version_id:
                raise DownstreamCommitConflict("History commit version differs")
            raw = body.read(MAX_RESULT_BYTES + 1)
            if len(raw) > MAX_RESULT_BYTES:
                raise DownstreamCommitConflict("History commit exceeds size limit")
        finally:
            body.close()

        if hashlib.sha256(raw).hexdigest() != reference.sha256:
            raise DownstreamCommitConflict("History commit checksum differs")

        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError) as error:
            raise DownstreamCommitConflict("Invalid History commit JSON") from error

        if not isinstance(document, dict):
            raise DownstreamCommitConflict("Expected a History commit object")
        return document

    def _read_completion(
        self, expected: CatalogueRegistration
    ) -> ProcessingCompletion | None:
        actual = self._catalogue.read(
            database=expected.database,
            table=expected.table,
        )
        if actual is None:
            return None
        if actual != expected:
            raise DownstreamCommitConflict(
                "Existing catalogue registration differs from the pinned request"
            )

        return ProcessingCompletion(
            operation_id=expected.operation_id,
            ingestion_id=expected.ingestion_id,
            request_fingerprint=expected.request_fingerprint,
            result_reference={
                "kind": "publication-result",
                "glue_database": expected.database,
                "glue_table": expected.table,
                "history_location": expected.history_location,
                "history_commit_id": expected.history_commit.sha256,
            },
        )