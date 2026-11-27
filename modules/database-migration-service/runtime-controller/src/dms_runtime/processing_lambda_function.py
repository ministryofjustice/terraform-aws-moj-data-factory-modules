import os
from functools import lru_cache
from typing import Any

import boto3

from dms_runtime.processing_component import ProcessingComponent
from dms_runtime.processing_dispatch import build_processing_stream_handler
from dms_runtime.standard_processing import build_standard_worker


@lru_cache(maxsize=1)
def _component() -> ProcessingComponent:
    table = boto3.resource("dynamodb").Table(
        os.environ["PROCESSING_OPERATIONS_TABLE_NAME"]
    )
    return ProcessingComponent(table)


@lru_cache(maxsize=1)
def _stream_handler():
    component = _component()
    worker = build_standard_worker(
        component=component,
        client=boto3.client("s3"),
        raw_bucket_owner=os.environ["PROCESSING_RAW_BUCKET_OWNER"],
        manifest_bucket_owner=os.environ["PROCESSING_MANIFEST_BUCKET_OWNER"],
        contract_bucket_owner=os.environ["PROCESSING_CONTRACT_BUCKET_OWNER"],
        output_bucket=os.environ["PROCESSING_OUTPUT_BUCKET"],
        output_bucket_owner=os.environ["PROCESSING_OUTPUT_BUCKET_OWNER"],
        output_prefix=os.environ["PROCESSING_OUTPUT_PREFIX"],
        max_batch_bytes=int(os.environ["PROCESSING_MAX_BATCH_BYTES"]),
    )
    return build_processing_stream_handler(
        worker=worker,
        lookup=component.handle,
        stream_arn=os.environ["PROCESSING_OPERATIONS_STREAM_ARN"],
    )


def receiver_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return _component().handle(event)


def stream_handler(
    event: dict[str, Any],
    context: Any,
) -> dict[str, list[dict[str, str]]]:
    return _stream_handler()(event, context)