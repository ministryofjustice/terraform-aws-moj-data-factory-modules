from unittest.mock import Mock

import pytest

from dms_runtime.processing_adapter import (
    BatchProcessingAdapter,
    ProcessingFailed,
    ProcessingProtocolError,
)


OPERATION_ID = "operation-one"
INGESTION_ID = "ingestion-one"
FINGERPRINT = "a" * 64

REQUEST = {
    "ingestion_id": INGESTION_ID,
    "deployment": {
        "configuration_revision": "b" * 64,
        "table": {"schema_name": "public", "table_name": "example"},
    },
    "contract": {"contract_object_version_id": "contract-version-one"},
    "raw_batch": {"version_id": "raw-batch-version-one"},
}

RESULT = {
    "bucket_name": "example-processing-results",
    "object_key": "processing/operation-one/result.json",
    "version_id": "result-version-one",
    "sha256": "c" * 64,
}


def response(status, **fields):
    return {
        "response_version": 1,
        "operation_id": OPERATION_ID,
        "ingestion_id": INGESTION_ID,
        "request_fingerprint": FINGERPRINT,
        "status": status,
        **fields,
    }


def submit(adapter):
    return adapter.submit(
        operation_id=OPERATION_ID,
        request_fingerprint=FINGERPRINT,
        request=REQUEST,
    )


def test_acceptance_passes_pinned_request_and_leaves_work_pending():
    invoke = Mock(return_value=response("ACCEPTED"))
    adapter = BatchProcessingAdapter(invoke)

    assert submit(adapter) is None

    invoke.assert_called_once_with(
        {
            "request_version": 1,
            "action": "submit_processing",
            "operation_id": OPERATION_ID,
            "request_fingerprint": FINGERPRINT,
            "request": REQUEST,
        }
    )


def test_lookup_recovers_the_original_result_after_lost_submit_response():
    invoke = Mock(
        side_effect=[
            TimeoutError("submit response lost"),
            response("SUCCEEDED", result=RESULT),
        ]
    )
    adapter = BatchProcessingAdapter(invoke)

    with pytest.raises(TimeoutError, match="submit response lost"):
        submit(adapter)

    completion = adapter.lookup(operation_id=OPERATION_ID)

    assert completion is not None
    assert completion.operation_id == OPERATION_ID
    assert completion.ingestion_id == INGESTION_ID
    assert completion.request_fingerprint == FINGERPRINT
    assert completion.result_reference == {"kind": "processing-result", **RESULT}
    assert [call.args[0]["action"] for call in invoke.call_args_list] == [
        "submit_processing",
        "lookup_processing",
    ]
    assert invoke.call_args.args[0] == {
        "request_version": 1,
        "action": "lookup_processing",
        "operation_id": OPERATION_ID,
    }


def test_unknown_operation_remains_unresolved():
    invoke = Mock(return_value=response("NOT_FOUND"))
    adapter = BatchProcessingAdapter(invoke)

    assert adapter.lookup(operation_id=OPERATION_ID) is None
    assert invoke.call_count == 1


@pytest.mark.parametrize(
    "fields, message",
    [
        ({"operation_id": "another-operation"}, "operation ID does not match"),
        ({"ingestion_id": "another-ingestion"}, "ingestion ID does not match"),
        ({"request_fingerprint": "d" * 64}, "fingerprint does not match"),
        ({"result": {"bucket_name": "example-results"}}, "versioned result"),
    ],
)
def test_submit_rejects_mismatched_or_incomplete_success(fields, message):
    result = response("SUCCEEDED", result=RESULT)
    result.update(fields)
    adapter = BatchProcessingAdapter(Mock(return_value=result))

    with pytest.raises(ProcessingProtocolError, match=message):
        submit(adapter)


def test_terminal_failure_is_reported_to_the_caller():
    adapter = BatchProcessingAdapter(Mock(return_value=response("FAILED")))

    with pytest.raises(ProcessingFailed, match=OPERATION_ID):
        submit(adapter)