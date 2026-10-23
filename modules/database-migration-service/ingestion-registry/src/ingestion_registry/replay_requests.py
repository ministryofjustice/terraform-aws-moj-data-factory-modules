import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from ingestion_registry.contracts import RegisteredContract
from ingestion_registry.identity import (
    TableIdentity,
    request_partition_key,
)
from ingestion_registry.references import S3ObjectReference


@dataclass(frozen=True)
class ReplayRequest:
    request_id: str
    table: TableIdentity
    source_ingestion_id: str
    history_reference: S3ObjectReference
    processing_revision: str
    contract: RegisteredContract

    def __post_init__(self) -> None:
        if not isinstance(self.table, TableIdentity):
            raise ValueError("table must be a TableIdentity")

        if (
            not isinstance(self.source_ingestion_id, str)
            or not self.source_ingestion_id
            or self.source_ingestion_id
            != self.source_ingestion_id.strip()
            or len(self.source_ingestion_id.encode("utf-8")) > 256
        ):
            raise ValueError(
                "source_ingestion_id must contain 1 to 256 UTF-8 "
                "bytes without surrounding whitespace"
            )

        if not isinstance(
            self.history_reference,
            S3ObjectReference,
        ):
            raise ValueError(
                "history_reference must be an S3ObjectReference"
            )

        if (
            not isinstance(self.processing_revision, str)
            or re.fullmatch(
                r"[a-f0-9]{64}",
                self.processing_revision,
            ) is None
        ):
            raise ValueError(
                "processing_revision must be a lowercase "
                "SHA-256 revision"
            )

        if not isinstance(self.contract, RegisteredContract):
            raise ValueError(
                "contract must be a RegisteredContract"
            )

        request_partition_key(
            environment=self.table.environment,
            source=self.table.source,
            request_id=self.request_id,
        )

    @property
    def partition_key(self) -> str:
        return request_partition_key(
            environment=self.table.environment,
            source=self.table.source,
            request_id=self.request_id,
        )

    def fingerprint_payload(self) -> dict[str, Any]:
        return {
            "request_version": 1,
            "operation": "REPLAY",
            "table": asdict(self.table),
            "source_ingestion_id": self.source_ingestion_id,
            "history_reference": asdict(self.history_reference),
            "processing_revision": self.processing_revision,
            "contract": asdict(self.contract),
        }

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.fingerprint_payload(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()