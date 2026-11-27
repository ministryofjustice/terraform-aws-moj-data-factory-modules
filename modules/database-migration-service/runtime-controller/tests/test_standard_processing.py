import hashlib
import json
from dataclasses import asdict
from functools import lru_cache

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from moto import mock_aws

from dms_runtime import processing_lambda_function
from dms_runtime.processing_adapter import BatchProcessingAdapter
from dms_runtime.processing_inputs import ProcessingInputError
from dms_runtime.standard_processing import (
    S3ParquetOutputWriter,
    inspect_parquet_batch,
)
from ingestion_registry.references import S3ObjectReference
from test_processing_component import OPERATION_ID, component_setup, submit


def make_parquet(columns):
    output = pa.BufferOutputStream()
    pq.write_table(pa.table(columns), output)
    return output.getvalue().to_pybytes()


def test_returns_row_counts_in_manifest_order():
    contract = json.dumps({
        "fields": [{"name": "id"}, {"name": "label"}],
    }).encode()

    first = make_parquet({"id": [1, 2], "label": ["a", "b"]})
    second = make_parquet({"label": ["c"], "id": [3]})

    assert inspect_parquet_batch(contract, (first, second)) == (2, 1)


def test_rejects_column_mismatch_in_a_later_file():
    contract = b'{"fields": [{"name": "id"}]}'
    first = make_parquet({"id": [1]})
    second = make_parquet({"unexpected": [2]})

    with pytest.raises(ProcessingInputError, match="entry 2 has different columns"):
        inspect_parquet_batch(contract, (first, second))


def test_rejects_invalid_parquet():
    contract = b'{"fields": [{"name": "id"}]}'

    with pytest.raises(ProcessingInputError, match="entry 1 is not readable Parquet"):
        inspect_parquet_batch(contract, (b"invalid parquet",))


@mock_aws
def test_output_writer_reuses_the_same_s3_version_on_retry():
    region = "eu-west-2"
    bucket = "processing-output"
    s3 = boto3.client("s3", region_name=region)
    owner = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    s3.create_bucket(
        Bucket=bucket,
        CreateBucketConfiguration={"LocationConstraint": region},
    )
    s3.put_bucket_versioning(
        Bucket=bucket,
        VersioningConfiguration={"Status": "Enabled"},
    )

    data = make_parquet({"id": [1, 2]})
    source = S3ObjectReference(
        bucket_name="raw-input",
        object_key="raw/LOAD00000001.parquet",
        version_id="pinned-raw-version",
        sha256=hashlib.sha256(data).hexdigest(),
    )
    writer = S3ParquetOutputWriter(
        s3,
        bucket_name=bucket,
        bucket_owner=owner,
        prefix="processing/",
    )

    first = writer(
        operation_id="a" * 64,
        raw_objects=((source, data),),
        row_counts=(2,),
    )
    replay = writer(
        operation_id="a" * 64,
        raw_objects=((source, data),),
        row_counts=(2,),
    )

    assert replay == first
    assert first["row_count"] == 2
    (output,) = first["objects"]
    assert output["row_count"] == 2
    assert output["sha256"] == source.sha256

    versions = s3.list_object_versions(Bucket=bucket, Prefix="processing/")
    (version,) = versions["Versions"]
    assert version["VersionId"] == output["version_id"]
    response = s3.get_object(
        Bucket=bucket,
        Key=output["object_key"],
        VersionId=output["version_id"],
    )
    with response["Body"] as body:
        assert body.read() == data


def test_standard_worker_processes_pinned_raw_version_and_replays(
    component_setup, monkeypatch
):
    _, request = component_setup
    region = "eu-west-2"
    bucket = "standard-worker-test"
    s3 = boto3.client("s3", region_name=region)
    owner = boto3.client("sts", region_name=region).get_caller_identity()["Account"]

    s3.create_bucket(
        Bucket=bucket,
        CreateBucketConfiguration={"LocationConstraint": region},
    )
    s3.put_bucket_versioning(
        Bucket=bucket,
        VersioningConfiguration={"Status": "Enabled"},
    )

    def put_version(key, data):
        response = s3.put_object(Bucket=bucket, Key=key, Body=data)
        return S3ObjectReference(
            bucket_name=bucket,
            object_key=key,
            version_id=response["VersionId"],
            sha256=hashlib.sha256(data).hexdigest(),
        )

    table = request["deployment"]["table"]
    contract = put_version(
        "contracts/example/db/public/example/v1/contract.avsc",
        json.dumps({
            "source": "example",
            "database_name": "db",
            "contract_version": "v1",
            **table,
            "fields": [{"name": "id", "type": "long"}],
        }).encode(),
    )

    data = make_parquet({"id": [1, 2]})
    raw = put_version("raw/LOAD00000001.parquet", data)
    manifest = put_version(
        "manifests/batch.json",
        json.dumps({
            "manifest_version": 1,
            "ingestion_id": request["ingestion_id"],
            "extraction_generation": 2,
            "table": table,
            "objects": [asdict(raw)],
        }).encode(),
    )
    put_version(raw.object_key, b"newer version outside the pinned manifest")

    request["deployment"].update({
        "source": "example",
        "database_name": "db",
        "extraction_generation": 2,
        "landing": {"bucket_name": bucket, "prefix": "raw/"},
    })
    request["contract"].update({
        "contract_version": "v1",
        "contract_bucket_name": contract.bucket_name,
        "contract_object_key": contract.object_key,
        "contract_object_version_id": contract.version_id,
        "contract_object_sha256": contract.sha256,
    })
    request["raw_batch"] = asdict(manifest)

    stream_arn = (
        f"arn:aws:dynamodb:{region}:{owner}:table/processing-operations"
        "/stream/2026-11-27T10:00:00.000"
    )
    settings = {
        "AWS_DEFAULT_REGION": region,
        "PROCESSING_OPERATIONS_TABLE_NAME": "processing-operations",
        "PROCESSING_OPERATIONS_STREAM_ARN": stream_arn,
        "PROCESSING_RAW_BUCKET_OWNER": owner,
        "PROCESSING_MANIFEST_BUCKET_OWNER": owner,
        "PROCESSING_CONTRACT_BUCKET_OWNER": owner,
        "PROCESSING_OUTPUT_BUCKET": bucket,
        "PROCESSING_OUTPUT_BUCKET_OWNER": owner,
        "PROCESSING_OUTPUT_PREFIX": "processing/",
        "PROCESSING_MAX_BATCH_BYTES": str(len(data)),
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)

    for name in ("_component", "_stream_handler"):
        cached = getattr(processing_lambda_function, name)
        monkeypatch.setattr(
            processing_lambda_function,
            name,
            lru_cache(maxsize=1)(cached.__wrapped__),
        )

    stream_event = {
        "Records": [{
            "eventName": "INSERT",
            "eventSourceARN": stream_arn,
            "dynamodb": {
                "SequenceNumber": "1",
                "Keys": {"operation_id": {"S": OPERATION_ID}},
            },
        }],
    }

    def invoke_receiver(event):
        return processing_lambda_function.receiver_handler(event, None)

    adapter = BatchProcessingAdapter(invoke_receiver)
    assert submit(adapter, request) is None

    assert processing_lambda_function.stream_handler(stream_event, None) == {
        "batchItemFailures": [],
    }

    completion = adapter.lookup(operation_id=OPERATION_ID)
    assert completion is not None
    reference = dict(completion.result_reference)
    assert reference.pop("kind") == "processing-result"
    result = S3ObjectReference(**reference)

    result_object = s3.get_object(
        Bucket=result.bucket_name,
        Key=result.object_key,
        VersionId=result.version_id,
    )
    with result_object["Body"] as body:
        result_bytes = body.read()
    assert hashlib.sha256(result_bytes).hexdigest() == result.sha256

    output = json.loads(result_bytes)["output"]
    assert output["row_count"] == 2
    (parquet,) = output["objects"]
    assert parquet["row_count"] == 2
    assert parquet["sha256"] == raw.sha256

    parquet_object = s3.get_object(
        Bucket=parquet["bucket_name"],
        Key=parquet["object_key"],
        VersionId=parquet["version_id"],
    )
    with parquet_object["Body"] as body:
        assert body.read() == data

    before = s3.list_object_versions(Bucket=bucket, Prefix="processing/")["Versions"]
    assert len(before) == 2
    assert submit(adapter, request) == completion
    assert processing_lambda_function.stream_handler(stream_event, None) == {
        "batchItemFailures": [],
    }
    after = s3.list_object_versions(Bucket=bucket, Prefix="processing/")["Versions"]
    assert after == before