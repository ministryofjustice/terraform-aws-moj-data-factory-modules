from dataclasses import asdict
import hashlib
import json
from unittest.mock import Mock

import boto3
import pytest
from moto import mock_aws

from dms_runtime.processing_adapter import BatchProcessingAdapter
from dms_runtime.processing_component import (
    ProcessingComponent,
    ProcessingOperationConflict,
)
from ingestion_registry.references import S3ObjectReference


INGESTION_ID = "ingestion-one"
RAW_BATCH = S3ObjectReference(
    bucket_name="example-raw",
    object_key="batches/ingestion-one.json",
    version_id="raw-version-one",
    sha256="a" * 64,
)
RESULT = S3ObjectReference(
    bucket_name="example-results",
    object_key="processing/ingestion-one/result.json",
    version_id="result-version-one",
    sha256="b" * 64,
)


def fingerprint(document):
    encoded = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


OPERATION_ID = fingerprint(
    {"ingestion_id": INGESTION_ID, "stage": "PROCESSING"}
)


@pytest.fixture
def component_setup():
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="eu-west-2")
        table = dynamodb.create_table(
            TableName="processing-operations",
            KeySchema=[{"AttributeName": "operation_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "operation_id", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        component = ProcessingComponent(table)
        request = {
            "ingestion_id": INGESTION_ID,
            "deployment": {
                "configuration_revision": "c" * 64,
                "table": {"schema_name": "public", "table_name": "example"},
            },
            "contract": {
                "kind": "registered-contract",
                "contract_bucket_name": "example-contracts",
                "contract_object_key": "source/db/public/example/v1/contract.avsc",
                "contract_object_version_id": "contract-version-one",
                "contract_object_sha256": "d" * 64,
            },
            "raw_batch": asdict(RAW_BATCH),
        }
        yield component, request


def submit(adapter, request):
    return adapter.submit(
        operation_id=OPERATION_ID,
        request_fingerprint=fingerprint(request),
        request=request,
    )


def test_worker_recovers_accepted_work_after_submit_response_is_lost(
    component_setup,
):
    component, request = component_setup
    processor = Mock(return_value=RESULT)

    def invoke(event):
        response = component.handle(event)
        if event["action"] == "submit_processing":
            raise TimeoutError("submit response lost")
        return response

    adapter = BatchProcessingAdapter(invoke)

    with pytest.raises(TimeoutError, match="submit response lost"):
        submit(adapter, request)

    claimed = component.claim_work(OPERATION_ID)
    assert claimed is not None
    token, pinned_request = claimed
    assert pinned_request == request
    assert component.claim_work(OPERATION_ID) is None

    component.complete(
        OPERATION_ID,
        worker_token=token,
        result=processor(pinned_request),
    )

    restarted_adapter = BatchProcessingAdapter(component.handle)
    recovered = restarted_adapter.lookup(operation_id=OPERATION_ID)

    assert recovered is not None
    assert recovered.ingestion_id == INGESTION_ID
    assert recovered.request_fingerprint == fingerprint(request)
    assert recovered.result_reference == {
        "kind": "processing-result",
        **asdict(RESULT),
    }
    assert submit(restarted_adapter, request) == recovered
    processor.assert_called_once_with(request)


def test_rejects_same_operation_id_with_different_inputs(component_setup):
    component, request = component_setup
    adapter = BatchProcessingAdapter(component.handle)

    assert submit(adapter, request) is None

    changed = {
        **request,
        "raw_batch": {**request["raw_batch"], "version_id": "different-version"},
    }
    with pytest.raises(ProcessingOperationConflict, match="different inputs"):
        submit(adapter, changed)

    claimed = component.claim_work(OPERATION_ID)
    assert claimed is not None
    assert claimed[1] == request


def test_only_claiming_worker_can_complete_and_exact_retry_is_safe(
    component_setup,
):
    component, request = component_setup
    adapter = BatchProcessingAdapter(component.handle)

    assert submit(adapter, request) is None
    claimed = component.claim_work(OPERATION_ID)
    assert claimed is not None
    token, _ = claimed

    with pytest.raises(ProcessingOperationConflict, match="cannot record"):
        component.complete(
            OPERATION_ID,
            worker_token="another-worker",
            result=RESULT,
        )

    assert adapter.lookup(operation_id=OPERATION_ID) is None

    component.complete(OPERATION_ID, worker_token=token, result=RESULT)
    component.complete(OPERATION_ID, worker_token=token, result=RESULT)

    result = adapter.lookup(operation_id=OPERATION_ID)
    assert result is not None
    assert result.result_reference["version_id"] == RESULT.version_id


def test_rejects_wrong_request_fingerprint_before_reserving(component_setup):
    component, request = component_setup

    with pytest.raises(ValueError, match="does not match the request"):
        component.handle(
            {
                "request_version": 1,
                "action": "submit_processing",
                "operation_id": OPERATION_ID,
                "request_fingerprint": "e" * 64,
                "request": request,
            }
        )

    adapter = BatchProcessingAdapter(component.handle)
    assert adapter.lookup(operation_id=OPERATION_ID) is None
    assert component.claim_work(OPERATION_ID) is None

def test_worker_failure_is_terminal_and_exact_retry_is_safe(component_setup):
    component, request = component_setup
    adapter = BatchProcessingAdapter(component.handle)
    assert submit(adapter, request) is None

    claimed = component.claim_work(OPERATION_ID)
    assert claimed is not None
    token, _ = claimed

    with pytest.raises(ProcessingOperationConflict, match="cannot record"):
        component.fail(
            OPERATION_ID,
            worker_token="other-worker",
            failure_code="INVALID_PINNED_INPUT",
        )

    for _ in range(2):
        component.fail(
            OPERATION_ID,
            worker_token=token,
            failure_code="INVALID_PINNED_INPUT",
        )

    assert component.claim_work(OPERATION_ID) is None

    response = component.handle(
        {
            "request_version": 1,
            "action": "lookup_processing",
            "operation_id": OPERATION_ID,
        }
    )
    assert response["status"] == "FAILED"
    assert response["failure_code"] == "INVALID_PINNED_INPUT"