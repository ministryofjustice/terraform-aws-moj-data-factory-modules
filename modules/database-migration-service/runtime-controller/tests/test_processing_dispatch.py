from unittest.mock import Mock

from dms_runtime.processing_dispatch import build_processing_stream_handler


OPERATION_ID = "a" * 64
STREAM_ARN = (
    "arn:aws:dynamodb:eu-west-2:123456789012:"
    "table/processing-operations/stream/2026-11-12T00:00:00.000"
)


def stream_record(sequence="101", *, event_name="INSERT"):
    return {
        "eventName": event_name,
        "eventSourceARN": STREAM_ARN,
        "dynamodb": {
            "SequenceNumber": sequence,
            "Keys": {"operation_id": {"S": OPERATION_ID}},
        },
    }


def build_handler(worker, lookup):
    return build_processing_stream_handler(
        worker=worker,
        lookup=lookup,
        stream_arn=STREAM_ARN,
    )


def test_dispatches_new_operation_and_ignores_status_updates():
    worker = Mock()
    lookup = Mock()

    result = build_handler(worker, lookup)(
        {"Records": [stream_record(), stream_record("102", event_name="MODIFY")]},
        None,
    )

    assert result == {"batchItemFailures": []}
    worker.run.assert_called_once_with(OPERATION_ID)
    worker.recover_written_result.assert_not_called()
    lookup.assert_not_called()


def test_repeated_delivery_can_recover_evidence_already_written():
    worker = Mock()
    worker.run.return_value = None
    lookup = Mock()

    result = build_handler(worker, lookup)(
        {"Records": [stream_record()]},
        None,
    )

    assert result == {"batchItemFailures": []}
    worker.recover_written_result.assert_called_once_with(OPERATION_ID)
    lookup.assert_not_called()


def test_completed_operation_acknowledges_repeated_delivery():
    worker = Mock()
    worker.run.return_value = None
    worker.recover_written_result.return_value = None
    lookup = Mock(return_value={"status": "SUCCEEDED"})

    result = build_handler(worker, lookup)(
        {"Records": [stream_record()]},
        None,
    )

    assert result == {"batchItemFailures": []}
    lookup.assert_called_once_with(
        {
            "request_version": 1,
            "action": "lookup_processing",
            "operation_id": OPERATION_ID,
        }
    )


def test_unresolved_claim_reports_first_failed_sequence():
    worker = Mock()
    worker.run.return_value = None
    worker.recover_written_result.return_value = None
    lookup = Mock(return_value={"status": "RUNNING"})

    result = build_handler(worker, lookup)(
        {"Records": [stream_record(), stream_record("102")]},
        None,
    )

    assert result == {
        "batchItemFailures": [{"itemIdentifier": "101"}],
    }
    worker.run.assert_called_once_with(OPERATION_ID)


def test_worker_failure_is_retried_from_its_stream_sequence():
    worker = Mock()
    worker.run.side_effect = TimeoutError("completion write interrupted")
    lookup = Mock()

    result = build_handler(worker, lookup)(
        {"Records": [stream_record()]},
        None,
    )

    assert result == {
        "batchItemFailures": [{"itemIdentifier": "101"}],
    }
    lookup.assert_not_called()