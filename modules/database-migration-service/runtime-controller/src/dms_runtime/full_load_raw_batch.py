import hashlib
import re
from datetime import datetime
from typing import Any

from ingestion_registry.references import S3ObjectReference


class RawBatchUnavailable(RuntimeError):
    """The exact full-load batch cannot currently be established."""


class DmsParquetFullLoadSelector:
    def __init__(
        self,
        client: Any,
        *,
        bucket_owner: str,
        max_objects: int = 1000,
        max_pages: int = 20,
        max_batch_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        if not re.fullmatch(r"[0-9]{12}", bucket_owner):
            raise ValueError("bucket_owner must contain 12 digits")

        for name, value in (
            ("max_objects", max_objects),
            ("max_pages", max_pages),
            ("max_batch_bytes", max_batch_bytes),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")

        self.client = client
        self.owner = bucket_owner
        self.max_objects = max_objects
        self.max_pages = max_pages
        self.max_bytes = max_batch_bytes

    def select(self, deployment, *, not_before: datetime):
        if (
            not isinstance(not_before, datetime)
            or not_before.utcoffset() is None
        ):
            raise ValueError("not_before must be timezone-aware")

        landing = deployment["landing"]
        table = deployment["table"]
        prefix = landing["prefix"]
        parts = [table["schema_name"], table["table_name"]]

        if (
            not isinstance(prefix, str)
            or not prefix.endswith("/")
            or "\\" in prefix
            or any(
                part in {"", ".", ".."}
                for part in prefix[:-1].split("/")
            )
            or any(
                not isinstance(part, str)
                or not part
                or part != part.strip()
                or "/" in part
                or "\\" in part
                or part in {".", ".."}
                for part in parts
            )
        ):
            raise ValueError("Unsupported pinned table landing path")

        bucket = landing["bucket_name"]
        prefix += "/".join(parts) + "/"

        arguments = {
            "Bucket": bucket,
            "Prefix": prefix,
            "ExpectedBucketOwner": self.owner,
            "MaxKeys": 1000,
        }
        entries = {}
        tokens = set()

        for _ in range(self.max_pages):
            page = self.client.list_objects_v2(**arguments)

            for entry in page.get("Contents", []):
                key = entry["Key"]
                if not key.startswith(prefix):
                    raise RawBatchUnavailable(
                        "S3 returned a key outside the table"
                    )

                name = key[len(prefix):]

                if re.fullmatch(
                    r"[0-9]{8}-[0-9]+\.parquet", name
                ):
                    continue

                if not re.fullmatch(
                    r"LOAD[0-9A-Fa-f]+\.parquet", name
                ):
                    raise RawBatchUnavailable(
                        f"Unsupported DMS object layout: {key}"
                    )

                if key in entries or len(entries) >= self.max_objects:
                    raise RawBatchUnavailable(
                        "Duplicate object or object budget exceeded"
                    )

                modified = entry.get("LastModified")
                if (
                    not isinstance(modified, datetime)
                    or modified.utcoffset() is None
                    or modified < not_before
                ):
                    raise RawBatchUnavailable(
                        "Full-load object predates the recorded start"
                    )

                if (
                    type(entry.get("Size")) is not int
                    or entry["Size"] <= 0
                ):
                    raise RawBatchUnavailable(
                        "Full-load object has invalid size"
                    )

                entries[key] = entry

            if not page.get("IsTruncated", False):
                break

            token = page.get("NextContinuationToken")
            if (
                not isinstance(token, str)
                or not token
                or token in tokens
            ):
                raise RawBatchUnavailable(
                    "Invalid S3 pagination token"
                )

            tokens.add(token)
            arguments["ContinuationToken"] = token
        else:
            raise RawBatchUnavailable(
                "S3 listing page budget exhausted"
            )

        if not entries:
            raise RawBatchUnavailable(
                "No full-load objects; empty-table evidence is required"
            )

        if sum(entry["Size"] for entry in entries.values()) > self.max_bytes:
            raise RawBatchUnavailable(
                "Raw batch exceeds byte budget"
            )

        references = []

        for key, entry in sorted(entries.items()):
            head = self.client.head_object(
                Bucket=bucket,
                Key=key,
                ExpectedBucketOwner=self.owner,
                IfMatch=entry["ETag"],
            )
            version = head.get("VersionId")

            if (
                not isinstance(version, str)
                or not version
                or version == "null"
            ):
                raise RawBatchUnavailable(
                    "Raw landing bucket must be versioned"
                )

            if head.get("ContentLength") != entry["Size"]:
                raise RawBatchUnavailable(
                    "Raw object changed during selection"
                )

            response = self.client.get_object(
                Bucket=bucket,
                Key=key,
                VersionId=version,
                ExpectedBucketOwner=self.owner,
            )
            body = response["Body"]
            digest = hashlib.sha256()
            size = 0

            try:
                if response.get("VersionId") != version:
                    raise RawBatchUnavailable(
                        "S3 returned a different object version"
                    )

                while chunk := body.read(1024 * 1024):
                    size += len(chunk)
                    if size > entry["Size"]:
                        raise RawBatchUnavailable(
                            "Raw object exceeds declared size"
                        )
                    digest.update(chunk)
            finally:
                body.close()

            if size != entry["Size"]:
                raise RawBatchUnavailable(
                    "Raw object length differs"
                )

            references.append(
                S3ObjectReference(
                    bucket_name=bucket,
                    object_key=key,
                    version_id=version,
                    sha256=digest.hexdigest(),
                )
            )

        return tuple(references)


class FullLoadRawBatchHandoff:
    def __init__(
        self,
        *,
        history,
        readiness,
        selector,
        producer,
    ):
        self.history = history
        self.readiness = readiness
        self.selector = selector
        self.producer = producer

    def advance(self, claim, *, start_operation_id: str):
        binding, boundary = self.history.full_load_start(
            ingestion_id=claim.ingestion_id,
            operation_id=start_operation_id,
        )

        completed = self.producer.completed(
            claim,
            deployment=binding,
        )
        if completed is not None:
            return completed

        selected = [
            {
                field: binding["table"][field]
                for field in ("schema_name", "table_name")
            }
        ]

        def require_ready():
            result = self.readiness.assess(
                binding,
                full_load_not_before=boundary,
                selected_tables=selected,
            )
            if (
                result.get("full_load_status") != "SUCCEEDED"
                or result.get("full_load_ready") is not True
                or result.get("task_requires_attention") is not False
            ):
                raise RawBatchUnavailable(
                    "The selected table full load is not ready"
                )

        require_ready()

        objects = self.producer.select_once(
            claim,
            deployment=binding,
            select=lambda: self.selector.select(
                binding,
                not_before=boundary,
            ),
        )

        require_ready()

        return self.producer.commit(
            claim,
            deployment=binding,
            objects=objects,
        )