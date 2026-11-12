import hashlib
from unittest.mock import Mock, patch

import boto3
import pytest

from dms_runtime.processing_adapter import BatchProcessingAdapter
from dms_runtime.processing_result_store import (
    ProcessingEvidenceConflict,
    S3ProcessingResultStore,
)
from dms_runtime.processing_worker import ProcessingWorker
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