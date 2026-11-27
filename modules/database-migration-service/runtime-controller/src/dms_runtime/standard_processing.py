import json
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict
from typing import Any

from botocore.exceptions import ClientError

from dms_runtime.processing_component import ProcessingComponent
from dms_runtime.processing_executor import BoundedS3ProcessingExecutor
from dms_runtime.processing_inputs import (
    MAX_CONTRACT_BYTES,
    ProcessingInputError,
    S3ProcessingInputVerifier,
    read_pinned_input,
)
from dms_runtime.processing_result_store import (
    ProcessingEvidenceConflict,
    S3ProcessingResultStore,
)
from dms_runtime.processing_worker import ProcessingWorker
from ingestion_registry.references import S3ObjectReference


def inspect_parquet_batch(
    contract_bytes: bytes,
    parquet_objects: tuple[bytes, ...],
) -> tuple[int, ...]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    try:
        fields = json.loads(contract_bytes)["fields"]
        if not isinstance(fields, list) or not fields:
            raise ValueError("Expected a non-empty list of contract fields")

        columns = [field["name"] for field in fields]
        if any(not isinstance(name, str) or not name for name in columns):
            raise ValueError("Expected non-empty contract column names")
        if len(set(columns)) != len(columns):
            raise ValueError("Duplicate contract column names")
    except (KeyError, TypeError, ValueError) as error:
        raise ProcessingInputError("Invalid registered contract fields") from error

    expected_columns = sorted(columns)
    row_counts = []
    for index, data in enumerate(parquet_objects, start=1):
        try:
            parquet = pq.ParquetFile(pa.BufferReader(data))
            actual_columns = parquet.schema_arrow.names
            row_count = parquet.metadata.num_rows
        except (pa.ArrowInvalid, OSError) as error:
            raise ProcessingInputError(
                f"Raw manifest entry {index} is not readable Parquet"
            ) from error

        if sorted(actual_columns) != expected_columns:
            raise ProcessingInputError(
                f"Raw manifest entry {index} has different columns "
                "from the registered contract"
            )

        row_counts.append(row_count)

    return tuple(row_counts)


def build_standard_executor(
    client: Any,
    *,
    raw_bucket_owner: str,
    contract_bucket_owner: str,
    max_batch_bytes: int,
    write_output: Callable[..., Mapping[str, Any]],
) -> BoundedS3ProcessingExecutor:

    def process(
        *,
        operation_id: str,
        request: Mapping[str, Any],
        contract: S3ObjectReference,
        raw_objects: tuple[tuple[S3ObjectReference, bytes], ...],
    ) -> Mapping[str, Any]:
        contract_bytes = read_pinned_input(
            client,
            contract,
            owner=contract_bucket_owner,
            limit=MAX_CONTRACT_BYTES,
            label="Registered contract",
            size_error="Registered contract exceeds the Processing byte limit",
        )
        row_counts = inspect_parquet_batch(
            contract_bytes,
            tuple(data for _, data in raw_objects),
        )

        return write_output(
            operation_id=operation_id,
            raw_objects=raw_objects,
            row_counts=row_counts,
        )

    return BoundedS3ProcessingExecutor(
        client,
        expected_bucket_owner=raw_bucket_owner,
        max_batch_bytes=max_batch_bytes,
        process=process,
    )


class S3ParquetOutputWriter:

    def __init__(
        self,
        client: Any,
        *,
        bucket_name: str,
        bucket_owner: str,
        prefix: str,
    ) -> None:
        if not bucket_name or bucket_name != bucket_name.strip():
            raise ValueError("A Processing output bucket is required")
        if re.fullmatch(r"[0-9]{12}", bucket_owner) is None:
            raise ValueError("Processing output bucket owner must be 12 digits")
        if (
            not prefix.endswith("/")
            or prefix != prefix.strip()
            or "\\" in prefix
            or any(part in {"", ".", ".."} for part in prefix[:-1].split("/"))
        ):
            raise ValueError("Use a relative Processing prefix ending in '/'")

        self._client = client
        self._bucket_name = bucket_name
        self._bucket_owner = bucket_owner
        self._prefix = prefix

    def __call__(
        self,
        *,
        operation_id: str,
        raw_objects: tuple[tuple[S3ObjectReference, bytes], ...],
        row_counts: tuple[int, ...],
    ) -> Mapping[str, Any]:
        if re.fullmatch(r"[0-9a-f]{64}", operation_id) is None:
            raise ValueError("Invalid Processing operation ID")
        if len(raw_objects) != len(row_counts):
            raise ValueError("Expected one row count per Raw object")

        objects = []
        for index, (source, data) in enumerate(raw_objects):
            key = f"{self._prefix}data/{operation_id}/{index:04d}.parquet"
            reference = self._write_object(key, data, source.sha256)
            objects.append({
                **asdict(reference),
                "row_count": row_counts[index],
            })

        return {
            "format": "parquet",
            "row_count": sum(row_counts),
            "objects": objects,
        }

    def _write_object(
        self,
        key: str,
        data: bytes,
        sha256: str,
    ) -> S3ObjectReference:
        location = {
            "Bucket": self._bucket_name,
            "Key": key,
            "ExpectedBucketOwner": self._bucket_owner,
        }
        try:
            response = self._client.put_object(
                **location,
                Body=data,
                ContentType="application/vnd.apache.parquet",
                IfNoneMatch="*",
            )
        except ClientError as error:
            if error.response["Error"]["Code"] not in {"PreconditionFailed", "412"}:
                raise
            response = self._client.head_object(**location)

        try:
            reference = S3ObjectReference(
                bucket_name=self._bucket_name,
                object_key=key,
                version_id=response.get("VersionId"),
                sha256=sha256,
            )
            read_pinned_input(
                self._client,
                reference,
                owner=self._bucket_owner,
                limit=len(data),
                label="Processing output",
                size_error="Processing output exceeds the expected size",
            )
        except ValueError as error:
            raise ProcessingEvidenceConflict(
                f"Cannot verify the versioned Processing output at {key}"
            ) from error

        return reference


def build_standard_worker(
    *,
    component: ProcessingComponent,
    client: Any,
    raw_bucket_owner: str,
    manifest_bucket_owner: str,
    contract_bucket_owner: str,
    output_bucket: str,
    output_bucket_owner: str,
    output_prefix: str,
    max_batch_bytes: int,
) -> ProcessingWorker:
    writer = S3ParquetOutputWriter(
        client,
        bucket_name=output_bucket,
        bucket_owner=output_bucket_owner,
        prefix=output_prefix,
    )
    verifier = S3ProcessingInputVerifier(
        client,
        raw_bucket_owner=raw_bucket_owner,
        manifest_bucket_owner=manifest_bucket_owner,
        contract_bucket_owner=contract_bucket_owner,
    )
    executor = build_standard_executor(
        client,
        raw_bucket_owner=raw_bucket_owner,
        contract_bucket_owner=contract_bucket_owner,
        max_batch_bytes=max_batch_bytes,
        write_output=writer,
    )
    results = S3ProcessingResultStore(
        client,
        bucket_name=output_bucket,
        prefix=f"{output_prefix}results/",
        expected_bucket_owner=output_bucket_owner,
    )
    return ProcessingWorker(
        component=component,
        verifier=verifier,
        executor=executor,
        results=results,
    )