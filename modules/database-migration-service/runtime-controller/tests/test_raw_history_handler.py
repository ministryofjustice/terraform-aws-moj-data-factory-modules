import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock

import boto3
import pytest

from dms_runtime.batch_component_adapters import LambdaRawHistoryAdapter
from dms_runtime.batch_processing import ProcessingCompletion
from dms_runtime.downstream_commit_adapter import DownstreamCommitConflict
from dms_runtime.raw_history_handler import RawHistoryHandler

from test_downstream_raw_manifest import context_request, target_for
from test_processing_inputs import pinned_inputs as pinned_inputs


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


@pytest.fixture
def setup(context_request):
    verifier, request, raw = context_request

    table = boto3.resource(
        "dynamodb",
        region_name="eu-west-2",
    ).create_table(
        TableName="raw-history-handler-intents",
        KeySchema=[
            {
                "AttributeName": "operation_id",
                "KeyType": "HASH",
            },
        ],
        AttributeDefinitions=[
            {
                "AttributeName": "operation_id",
                "AttributeType": "S",
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    operation = digest({
        "ingestion_id": request["ingestion_id"],
        "stage": "HISTORY_BASELINE",
    })
    fingerprint = digest(request)

    event = {
        "request_version": 1,
        "action": "submit_raw_history",
        "operation_id": operation,
        "request_fingerprint": fingerprint,
        "request": request,
    }

    completion = ProcessingCompletion(
        operation_id=operation,
        ingestion_id=request["ingestion_id"],
        request_fingerprint=fingerprint,
        result_reference={
            "kind": "raw-history-commit",
            "commit_id": "c" * 64,
            "history_location": (
                "s3://history-bucket/domain/table/version=2/"
            ),
            "bucket_name": "history-bucket",
            "object_key": "commits/example.json",
            "version_id": "version-one",
            "sha256": "c" * 64,
        },
    )

    target = Mock()
    target.lookup.return_value = None
    target.commit.return_value = completion

    scope = Mock()
    resolver = Mock(return_value=target)

    handler = RawHistoryHandler(
        table=table,
        resolve_target=resolver,
        check_scope=scope,
        clock=lambda: 1000,
    )

    return {
        "handler": handler,
        "table": table,
        "target": target,
        "scope": scope,
        "resolver": resolver,
        "event": event,
        "completion": completion,
        "verifier": verifier,
        "raw": raw,
    }


def lookup(event):
    return {
        "request_version": 1,
        "action": "lookup_raw_history",
        "operation_id": event["operation_id"],
    }


def test_submit_returns_verified_completion(setup):
    result = setup["handler"].handle(setup["event"])

    assert result["status"] == "SUCCEEDED"
    assert result["result_reference"] == (
        setup["completion"].result_reference
    )

    stored = setup["table"].get_item(
        Key={
            "operation_id": setup["event"]["operation_id"],
        },
    )["Item"]

    assert json.loads(stored["request_json"]) == (
        setup["event"]["request"]
    )
    assert stored["component"] == "raw_history"

    setup["scope"].assert_called_once_with(
        setup["event"]["request"]
    )


def test_pending_target_is_accepted_not_completed(setup):
    setup["target"].commit.return_value = None

    result = setup["handler"].handle(setup["event"])

    assert result["status"] == "ACCEPTED"
    assert "result_reference" not in result
    assert setup["table"].scan()["Count"] == 1


def test_missing_lookup_does_not_create_work(setup):
    result = setup["handler"].handle(lookup(setup["event"]))

    assert result["status"] == "NOT_FOUND"
    assert setup["table"].scan()["Count"] == 0
    setup["resolver"].assert_not_called()


def test_lookup_recovers_completion_without_another_write(setup):
    setup["handler"].handle(setup["event"])
    setup["target"].lookup.return_value = setup["completion"]
    before = setup["table"].scan()["Items"]

    result = setup["handler"].handle(lookup(setup["event"]))

    assert result["status"] == "SUCCEEDED"
    setup["target"].commit.assert_called_once()
    assert setup["table"].scan()["Items"] == before


@pytest.mark.parametrize(
    "change",
    [
        {"request_version": True},
        {"action": "delete_history"},
        {"operation_id": "invalid"},
        {"unexpected": True},
        {"request_fingerprint": "a" * 64},
    ],
)
def test_invalid_envelope_rejected_before_writes(setup, change):
    with pytest.raises(ValueError):
        setup["handler"].handle({
            **setup["event"],
            **change,
        })

    assert setup["table"].scan()["Count"] == 0
    setup["target"].commit.assert_not_called()


def test_missing_context_rejected_before_writes(setup):
    event = deepcopy(setup["event"])
    del event["request"]["execution_context"]

    with pytest.raises(ValueError, match="execution context"):
        setup["handler"].handle(event)

    assert setup["table"].scan()["Count"] == 0


def test_scope_rejection_prevents_acceptance(setup):
    setup["scope"].side_effect = PermissionError(
        "outside configured service"
    )

    with pytest.raises(PermissionError):
        setup["handler"].handle(setup["event"])

    assert setup["table"].scan()["Count"] == 0
    setup["resolver"].assert_not_called()


def test_lookup_rechecks_scope(setup):
    setup["handler"].handle(setup["event"])
    setup["target"].reset_mock()
    setup["scope"].side_effect = PermissionError("scope rejected")

    with pytest.raises(PermissionError):
        setup["handler"].handle(lookup(setup["event"]))

    setup["target"].lookup.assert_not_called()


def test_changed_request_cannot_reuse_operation(setup):
    setup["target"].commit.return_value = None
    setup["handler"].handle(setup["event"])

    changed = deepcopy(setup["event"])
    changed["request"]["raw_batch"]["sha256"] = "d" * 64
    changed["request_fingerprint"] = digest(changed["request"])

    with pytest.raises(
        DownstreamCommitConflict,
        match="different inputs",
    ):
        setup["handler"].handle(changed)

    setup["target"].commit.assert_called_once()


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "processing-result"},
        {"version_id": "null"},
        {"commit_id": "d" * 64},
        {"history_location": "https://example.invalid/"},
    ],
)
def test_invalid_completion_never_returns_success(setup, change):
    completion = setup["completion"]
    setup["target"].commit.return_value = replace(
        completion,
        result_reference={
            **completion.result_reference,
            **change,
        },
    )

    with pytest.raises(DownstreamCommitConflict):
        setup["handler"].handle(setup["event"])


def test_oversized_request_rejected_before_writes(setup):
    event = deepcopy(setup["event"])
    event["request"]["raw_batch"]["object_key"] = "x" * (128 * 1024)

    with pytest.raises(ValueError, match="size limit"):
        setup["handler"].handle(event)

    assert setup["table"].scan()["Count"] == 0


def test_lookup_rejects_another_components_operation(setup):
    setup["table"].put_item(
        Item={
            "operation_id": setup["event"]["operation_id"],
            "component": "processing",
        },
    )

    with pytest.raises(
        DownstreamCommitConflict,
        match="another component",
    ):
        setup["handler"].handle(lookup(setup["event"]))

    setup["resolver"].assert_not_called()


def test_adapter_handler_and_real_archive_recover_lost_response(setup):
    request = setup["event"]["request"]
    s3 = boto3.client("s3", region_name="us-east-1")

    s3.create_bucket(Bucket="versioned-history-test")
    s3.put_bucket_versioning(
        Bucket="versioned-history-test",
        VersioningConfiguration={"Status": "Enabled"},
    )

    real = target_for(request, setup["verifier"])
    target = Mock(wraps=real)

    def lose_response(**arguments):
        real.commit(**arguments)
        raise TimeoutError("response lost after archive commit")

    target.commit.side_effect = lose_response

    handler = RawHistoryHandler(
        table=setup["table"],
        resolve_target=lambda _: target,
        check_scope=setup["scope"],
        clock=lambda: 1000,
    )
    adapter = LambdaRawHistoryAdapter(handler.handle)

    arguments = {
        "operation_id": setup["event"]["operation_id"],
        "request_fingerprint": setup["event"]["request_fingerprint"],
        "request": request,
    }

    with pytest.raises(TimeoutError):
        adapter.submit(**arguments)

    before = s3.list_object_versions(
        Bucket="versioned-history-test"
    )["Versions"]

    completion = adapter.lookup(
        operation_id=arguments["operation_id"]
    )

    assert completion is not None
    assert completion.result_reference["history_location"].endswith(
        "/version=2/"
    )
    target.commit.assert_called_once()

    assert s3.list_object_versions(
        Bucket="versioned-history-test"
    )["Versions"] == before

    with s3.get_object(
        Bucket=completion.result_reference["bucket_name"],
        Key=completion.result_reference["object_key"],
        VersionId=completion.result_reference["version_id"],
    )["Body"] as body:
        output = json.load(body)["output"]

    assert output["objects"][0]["source"]["version_id"] == (
        setup["raw"].version_id
    )


def test_pending_lookup_reads_without_committing_again(setup):
    setup["target"].commit.return_value = None
    setup["handler"].handle(setup["event"])
    before = setup["table"].scan()["Items"]

    result = setup["handler"].handle(lookup(setup["event"]))

    assert result["status"] == "ACCEPTED"
    assert "result_reference" not in result
    setup["target"].commit.assert_called_once()
    assert setup["table"].scan()["Items"] == before