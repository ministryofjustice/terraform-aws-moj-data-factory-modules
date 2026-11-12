import hashlib
import json
from dataclasses import asdict, replace
from unittest.mock import Mock

import boto3
import pytest
from moto import mock_aws

from dms_runtime.processing_inputs import (
    ProcessingInputError,
    S3ProcessingInputVerifier,
)
from dms_runtime.processing_worker import ProcessingWorker
from ingestion_registry.references import S3ObjectReference
from dms_runtime.processing_executor import BoundedS3ProcessingExecutor


@pytest.fixture
def pinned_inputs():
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        owner = boto3.client("sts").get_caller_identity()["Account"]
        bucket = "processing-inputs-test"

        s3.create_bucket(Bucket=bucket)
        s3.put_bucket_versioning(
            Bucket=bucket,
            VersioningConfiguration={"Status": "Enabled"},
        )

        def put(key, raw):
            response = s3.put_object(Bucket=bucket, Key=key, Body=raw)
            return S3ObjectReference(
                bucket_name=bucket,
                object_key=key,
                version_id=response["VersionId"],
                sha256=hashlib.sha256(raw).hexdigest(),
            )

        def put_json(key, document):
            return put(key, json.dumps(document).encode("utf-8"))

        table = {"schema_name": "public", "table_name": "example"}
        prefix = "landing/generation-one/"
        raw = put(f"{prefix}part-000.parquet", b"original fixture data")
        put(raw.object_key, b"newer fixture data")

        contract = put_json(
            "contracts/example/v1/contract.avsc",
            {
                "source": "sandbox-postgres",
                "database_name": "dms_test",
                "contract_version": "v1",
                **table,
            },
        )
        put_json(contract.object_key, {"contract_version": "v2"})

        manifest = {
            "manifest_version": 1,
            "ingestion_id": "ingestion-one",
            "extraction_generation": "generation-one",
            "table": table,
            "objects": [asdict(raw)],
        }
        manifest_ref = put_json("manifests/ingestion-one.json", manifest)
        put_json(manifest_ref.object_key, {"manifest_version": 2})

        request = {
            "ingestion_id": "ingestion-one",
            "deployment": {
                "source": "sandbox-postgres",
                "database_name": "dms_test",
                "extraction_generation": "generation-one",
                "table": table,
                "landing": {"bucket_name": bucket, "prefix": prefix},
            },
            "contract": {
                "kind": "registered-contract",
                "contract_version": "v1",
                "contract_bucket_name": contract.bucket_name,
                "contract_object_key": contract.object_key,
                "contract_object_version_id": contract.version_id,
                "contract_object_sha256": contract.sha256,
            },
            "raw_batch": asdict(manifest_ref),
        }
        verifier = S3ProcessingInputVerifier(
            s3,
            raw_bucket_owner=owner,
            contract_bucket_owner=owner,
        )
        yield verifier, request, raw, manifest, put_json


def test_worker_passes_pinned_object_version_to_executor(pinned_inputs):
    verifier, request, raw, _, _ = pinned_inputs
    component = Mock()
    component.claim_work.return_value = ("worker-token", request)
    executor = Mock()
    executor.execute.return_value = {"output_manifest_id": "output-one"}
    results = Mock()
    results.write.return_value = raw
    worker = ProcessingWorker(
        component=component,
        verifier=verifier,
        executor=executor,
        results=results,
    )

    assert worker.run("operation-one") == raw

    inputs = executor.execute.call_args.kwargs["inputs"]
    assert inputs.raw_objects == (raw,)
    assert inputs.raw_manifest.version_id == request["raw_batch"]["version_id"]
    assert inputs.contract.version_id == (
        request["contract"]["contract_object_version_id"]
    )
    component.complete.assert_called_once_with(
        "operation-one", worker_token="worker-token", result=raw
    )


def test_invalid_checksum_stops_worker_before_execution(pinned_inputs):
    verifier, request, _, _, _ = pinned_inputs
    request["raw_batch"]["sha256"] = "0" * 64
    component = Mock()
    component.claim_work.return_value = ("worker-token", request)
    executor = Mock()
    results = Mock()
    worker = ProcessingWorker(
        component=component,
        verifier=verifier,
        executor=executor,
        results=results,
    )

    with pytest.raises(ProcessingInputError, match="Raw manifest checksum"):
        worker.run("operation-one")

    executor.execute.assert_not_called()
    results.write.assert_not_called()
    component.complete.assert_not_called()
    component.fail.assert_called_once_with(
        "operation-one",
        worker_token="worker-token",
        failure_code="INVALID_PINNED_INPUT",
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"ingestion_id": "another-ingestion"},
        {"extraction_generation": "generation-two"},
        {"table": {"schema_name": "public", "table_name": "another_table"}},
    ],
)
def test_rejects_manifest_for_another_ingestion_boundary(pinned_inputs, changes):
    verifier, request, _, manifest, put_json = pinned_inputs
    request["raw_batch"] = asdict(
        put_json("manifests/incorrect.json", {**manifest, **changes})
    )

    with pytest.raises(ProcessingInputError, match="pinned ingestion"):
        verifier.verify(request)


def test_rejects_raw_object_outside_landing_prefix(pinned_inputs):
    verifier, request, raw, manifest, put_json = pinned_inputs
    wrong = replace(raw, object_key="landing/generation-one-other/part.parquet")
    request["raw_batch"] = asdict(
        put_json("manifests/outside.json", {**manifest, "objects": [asdict(wrong)]})
    )

    with pytest.raises(ProcessingInputError, match="outside the pinned landing"):
        verifier.verify(request)


def test_accepts_explicitly_empty_raw_batch(pinned_inputs):
    verifier, request, _, manifest, put_json = pinned_inputs
    request["raw_batch"] = asdict(
        put_json("manifests/empty.json", {**manifest, "objects": []})
    )

    assert verifier.verify(request).raw_objects == ()

def test_executor_reads_pinned_raw_version_and_rejects_a_bad_checksum(
    pinned_inputs,
):
    verifier, request, raw, _, _ = pinned_inputs
    processor = Mock(return_value={"output_manifest_id": "output-one"})
    executor = BoundedS3ProcessingExecutor(
        boto3.client("s3", region_name="us-east-1"),
        expected_bucket_owner=boto3.client("sts").get_caller_identity()["Account"],
        max_batch_bytes=1024,
        process=processor,
    )
    inputs = verifier.verify(request)

    assert executor.execute(
        operation_id="operation-one",
        request=request,
        inputs=inputs,
    ) == {"output_manifest_id": "output-one"}

    processor.assert_called_once_with(
        operation_id="operation-one",
        request=request,
        contract=inputs.contract,
        raw_objects=((raw, b"original fixture data"),),
    )

    processor.reset_mock()
    bad_inputs = replace(inputs, raw_objects=(replace(raw, sha256="0" * 64),))

    with pytest.raises(ProcessingInputError, match="Raw object checksum"):
        executor.execute(
            operation_id="operation-one",
            request=request,
            inputs=bad_inputs,
        )

    processor.assert_not_called()


def test_executor_reads_pinned_raw_version_and_rejects_a_bad_checksum(
    pinned_inputs,
):
    verifier, request, raw, _, _ = pinned_inputs
    processor = Mock(return_value={"output_manifest_id": "output-one"})
    executor = BoundedS3ProcessingExecutor(
        boto3.client("s3", region_name="us-east-1"),
        expected_bucket_owner=boto3.client("sts").get_caller_identity()["Account"],
        max_batch_bytes=1024,
        process=processor,
    )
    inputs = verifier.verify(request)

    assert executor.execute(
        operation_id="operation-one",
        request=request,
        inputs=inputs,
    ) == {"output_manifest_id": "output-one"}

    processor.assert_called_once_with(
        operation_id="operation-one",
        request=request,
        contract=inputs.contract,
        raw_objects=((raw, b"original fixture data"),),
    )

    processor.reset_mock()
    bad_inputs = replace(inputs, raw_objects=(replace(raw, sha256="0" * 64),))

    with pytest.raises(ProcessingInputError, match="Raw object checksum"):
        executor.execute(
            operation_id="operation-one",
            request=request,
            inputs=bad_inputs,
        )

    processor.assert_not_called()