import hashlib
from unittest.mock import Mock, patch

import boto3
import pytest
from botocore.exceptions import ClientError

from dms_runtime.processing_adapter import BatchProcessingAdapter
from dms_runtime.processing_dispatch import build_processing_stream_handler
from dms_runtime.processing_inputs import S3ProcessingInputVerifier
from dms_runtime.processing_result_store import (
    ProcessingEvidenceConflict,
    S3ProcessingResultStore,
)
from dms_runtime.processing_worker import ProcessingWorker
from ingestion_registry.references import S3ObjectReference
from test_processing_component import (
    OPERATION_ID,
    component_setup as component_setup,
    submit,
)


def test_recovers_written_result_without_executing_processing_again(
    component_setup,
):
    component, request = component_setup
    s3 = boto3.client("s3", region_name="eu-west-2")
    bucket = "processing-evidence-test"
    s3.create_bucket(
        Bucket=bucket,
        CreateBucketConfiguration={"LocationConstraint": "eu-west-2"},
    )
    s3.put_bucket_versioning(
        Bucket=bucket,
        VersioningConfiguration={"Status": "Enabled"},
    )
    account_id = boto3.client("sts", region_name="eu-west-2").get_caller_identity()[
        "Account"
    ]

    verified_inputs = Mock()
    verifier = Mock()
    verifier.verify.return_value = verified_inputs

    def build_worker(executor):
        return ProcessingWorker(
            component=component,
            verifier=verifier,
            executor=executor,
            results=S3ProcessingResultStore(
                s3,
                bucket_name=bucket,
                prefix="processing/",
                expected_bucket_owner=account_id,
            ),
        )

    adapter = BatchProcessingAdapter(component.handle)
    assert submit(adapter, request) is None

    executor = Mock()
    executor.execute.return_value = {"output_manifest_id": "output-one"}
    worker = build_worker(executor)

    with patch.object(
        component,
        "complete",
        side_effect=TimeoutError("completion write interrupted"),
    ):
        with pytest.raises(TimeoutError, match="completion write interrupted"):
            worker.run(OPERATION_ID)

    assert adapter.lookup(operation_id=OPERATION_ID) is None
    versions_before = s3.list_object_versions(Bucket=bucket)["Versions"]
    assert len(versions_before) == 1
    original = versions_before[0]

    recovery_executor = Mock()
    recovered = build_worker(recovery_executor).recover_written_result(
        OPERATION_ID
    )

    assert recovered is not None
    assert recovered.object_key == original["Key"]
    assert recovered.version_id == original["VersionId"]

    body = s3.get_object(
        Bucket=bucket,
        Key=original["Key"],
        VersionId=original["VersionId"],
    )["Body"]
    try:
        assert recovered.sha256 == hashlib.sha256(body.read()).hexdigest()
    finally:
        body.close()

    completion = adapter.lookup(operation_id=OPERATION_ID)
    assert completion is not None
    assert completion.result_reference["version_id"] == recovered.version_id
    assert completion.result_reference["sha256"] == recovered.sha256
    assert s3.list_object_versions(Bucket=bucket)["Versions"] == versions_before
    executor.execute.assert_called_once_with(
        operation_id=OPERATION_ID,
        request=request,
        inputs=verified_inputs,
    )
    verifier.verify.assert_called_once_with(request)
    recovery_executor.execute.assert_not_called()


@pytest.mark.parametrize(
    "write_conflicting_evidence",
    [False, True],
    ids=["missing-evidence", "different-raw-batch"],
)
def test_recovery_requires_evidence_for_the_pinned_request(
    component_setup,
    write_conflicting_evidence,
):
    component, request = component_setup
    adapter = BatchProcessingAdapter(component.handle)
    assert submit(adapter, request) is None

    claimed = component.claim_work(OPERATION_ID)
    assert claimed is not None
    worker_token, pinned_request = claimed

    s3 = boto3.client("s3", region_name="eu-west-2")
    bucket = "processing-recovery-test"
    s3.create_bucket(
        Bucket=bucket,
        CreateBucketConfiguration={"LocationConstraint": "eu-west-2"},
    )
    s3.put_bucket_versioning(
        Bucket=bucket,
        VersioningConfiguration={"Status": "Enabled"},
    )
    account_id = boto3.client("sts", region_name="eu-west-2").get_caller_identity()[
        "Account"
    ]
    results = S3ProcessingResultStore(
        s3,
        bucket_name=bucket,
        prefix="processing/",
        expected_bucket_owner=account_id,
    )
    executor = Mock()
    verifier = Mock()
    worker = ProcessingWorker(
        component=component,
        verifier=verifier,
        executor=executor,
        results=results,
    )

    with patch.object(component, "complete", wraps=component.complete) as complete:
        if write_conflicting_evidence:
            conflicting_request = {
                **pinned_request,
                "raw_batch": {
                    **pinned_request["raw_batch"],
                    "version_id": "different-raw-version",
                },
            }
            results.write(
                operation_id=OPERATION_ID,
                worker_token=worker_token,
                request=conflicting_request,
                output={"output_manifest_id": "output-one"},
            )
            with pytest.raises(
                ProcessingEvidenceConflict,
                match="does not match the claim and pinned request",
            ):
                worker.recover_written_result(OPERATION_ID)
        else:
            assert worker.recover_written_result(OPERATION_ID) is None

        complete.assert_not_called()

    executor.execute.assert_not_called()
    verifier.verify.assert_not_called()
    status = component.handle(
        {
            "request_version": 1,
            "action": "lookup_processing",
            "operation_id": OPERATION_ID,
        }
    )
    assert status["status"] == "RUNNING"


@pytest.mark.parametrize(
    "error_code",
    ["SlowDown", "NoSuchVersion"],
    ids=["retryable-read", "missing-pinned-version"],
)
def test_processing_dispatch_handles_pinned_read_failure(
    component_setup,
    error_code,
):
    component, original_request = component_setup
    request = {
        **original_request,
        "deployment": {
            **original_request["deployment"],
            "source": "sandbox-postgres",
            "database_name": "dms_test",
            "extraction_generation": "generation-one",
            "table": {
                "schema_name": "public",
                "table_name": "example",
            },
            "landing": {
                "bucket_name": original_request["raw_batch"]["bucket_name"],
                "prefix": "landing/generation-one/",
            },
        },
        "contract": {
            **original_request["contract"],
            "contract_version": "v1",
        },
    }
    assert submit(BatchProcessingAdapter(component.handle), request) is None

    owner = boto3.client("sts").get_caller_identity()["Account"]
    s3 = Mock()
    s3.get_object.side_effect = ClientError(
        {"Error": {"Code": error_code, "Message": error_code}},
        "GetObject",
    )
    verifier = S3ProcessingInputVerifier(
        s3,
        raw_bucket_owner=owner,
        contract_bucket_owner=owner,
    )
    executor = Mock()
    executor.execute.return_value = {"output_manifest_id": "output-one"}
    results = Mock()
    result_reference = S3ObjectReference(
        bucket_name="processing-evidence-test",
        object_key="processing/result.json",
        version_id="result-version",
        sha256="a" * 64,
    )
    results.write.return_value = result_reference
    results.find_verified.return_value = None
    worker = ProcessingWorker(
        component=component,
        verifier=verifier,
        executor=executor,
        results=results,
    )
    stream_arn = (
        "arn:aws:dynamodb:eu-west-2:123456789012:table/test/stream/test"
    )
    handler = build_processing_stream_handler(
        worker=worker,
        lookup=component.handle,
        stream_arn=stream_arn,
    )
    event = {
        "Records": [
            {
                "eventSourceARN": stream_arn,
                "eventName": "INSERT",
                "dynamodb": {
                    "SequenceNumber": "123",
                    "Keys": {"operation_id": {"S": OPERATION_ID}},
                },
            }
        ]
    }
    lookup_event = {
        "request_version": 1,
        "action": "lookup_processing",
        "operation_id": OPERATION_ID,
    }

    assert handler(event, None) == {
        "batchItemFailures": [{"itemIdentifier": "123"}]
    }
    s3.get_object.assert_called_once()
    executor.execute.assert_not_called()
    results.write.assert_not_called()
    state = component.handle(lookup_event)

    if error_code == "NoSuchVersion":
        assert state["status"] == "FAILED"
        assert state["failure_code"] == "INVALID_PINNED_INPUT"
        assert handler(event, None) == {"batchItemFailures": []}
        assert s3.get_object.call_count == 1
        executor.execute.assert_not_called()
        results.write.assert_not_called()
        return

    assert state["status"] == "ACCEPTED"

    # The first delivery uses the real verifier. On redelivery, supply
    # verified inputs to exercise the claim, execution and completion path.
    verified_inputs = Mock()
    with patch.object(verifier, "verify", return_value=verified_inputs) as verify:
        assert handler(event, None) == {"batchItemFailures": []}

    verify.assert_called_once_with(request)
    executor.execute.assert_called_once_with(
        operation_id=OPERATION_ID,
        request=request,
        inputs=verified_inputs,
    )
    results.write.assert_called_once()
    assert component.handle(lookup_event)["status"] == "SUCCEEDED"


def test_stale_processing_worker_cannot_requeue_a_new_claim(component_setup):
    component, request = component_setup
    assert submit(BatchProcessingAdapter(component.handle), request) is None

    first = component.claim_work(OPERATION_ID)
    assert first is not None
    first_token, _ = first
    assert component.requeue(OPERATION_ID, worker_token=first_token)

    second = component.claim_work(OPERATION_ID)
    assert second is not None
    second_token, _ = second
    assert second_token != first_token

    assert component.requeue(OPERATION_ID, worker_token=first_token) is False
    assert component.read_running_work(OPERATION_ID) == second