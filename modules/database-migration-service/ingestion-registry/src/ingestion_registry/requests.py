import hashlib
import json
import re
from dataclasses import asdict, dataclass

from ingestion_registry.identity import (
    TableIdentity,
    request_partition_key,
    validate_slug,
)


NEW_INGESTION_OPERATIONS = frozenset(
    {
        "INITIAL_INGESTION",
        "REINGESTION",
    }
)


def validate_revision(value: str, field: str) -> None:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[a-f0-9]{64}", value) is None
    ):
        raise ValueError(
            f"{field} must contain 64 lowercase hexadecimal characters"
        )


@dataclass(frozen=True)
class NewIngestionRequest:
    request_id: str
    operation: str
    table: TableIdentity
    configuration_revision: str
    artifact_revision: str
    extraction_generation: str

    def __post_init__(self) -> None:
        if not isinstance(self.table, TableIdentity):
            raise ValueError("table must be a TableIdentity")

        if (
            not isinstance(self.operation, str)
            or self.operation not in NEW_INGESTION_OPERATIONS
        ):
            raise ValueError(
                "operation must be INITIAL_INGESTION or REINGESTION"
            )

        # Validate the request ID using the same rules as its key
        request_partition_key(
            environment=self.table.environment,
            source=self.table.source,
            request_id=self.request_id,
        )

        validate_revision(
            self.configuration_revision,
            "configuration_revision",
        )
        validate_revision(
            self.artifact_revision,
            "artifact_revision",
        )
        validate_slug(
            self.extraction_generation,
            "extraction_generation",
        )

    @property
    def partition_key(self) -> str:
        return request_partition_key(
            environment=self.table.environment,
            source=self.table.source,
            request_id=self.request_id,
        )

    def fingerprint_payload(self) -> dict:
        return {
            "request_version": 1,
            "operation": self.operation,
            "table": asdict(self.table),
            "configuration_revision": self.configuration_revision,
            "artifact_revision": self.artifact_revision,
            "extraction_generation": self.extraction_generation,
        }

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.fingerprint_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")

        return hashlib.sha256(encoded).hexdigest()