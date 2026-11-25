import io
import json
from unittest.mock import Mock

import pytest

from dms_runtime import batch_bootstrap
from dms_runtime.batch_component_adapters import (
    ComponentFailed,
    ComponentInvocationError,
    ComponentProtocolError,
    ComponentTargetUnavailable,
    build_component_factories,
    read_component_targets,
)
from dms_runtime.ownership import RuntimeContext
from dms_runtime.processing_adapter import BatchProcessingAdapter

from test_batch_bootstrap import bootstrap_setup
from test_downstream_raw_manifest import context_request
from test_processing_inputs import pinned_inputs as pinned_inputs


CONTEXT = RuntimeContext(
    environment="development",
    account_id="111122223333",
    region="eu-west-2",
    source="example",
    database_name="example",
)
ARN = "arn:aws:lambda:eu-west-2:111122223333:function:component:3"
OPERATION = "a" * 64
FINGERPRINT = "b" * 64


def environment(component="raw_history"):
    return {
        "BATCH_CONTEXT_VERSION": "1",
        "BATCH_COMPONENT_TARGETS": json.dumps({
            component: [ARN],
        }),
    }


def response(status="SUCCEEDED"):
    return {
        "response_version": 1,
        "operation_id": OPERATION,
        "ingestion_id": "ingestion-one",
        "request_fingerprint": FINGERPRINT,
        "status": status,
        "result_reference": {
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
    }


def wire(document):
    return {
        "StatusCode": 200,
        "Payload": io.BytesIO(json.dumps(document).encode()),
    }


def adapter(client, component="raw_history"):
    factories = build_component_factories(
        client=client,
        context=CONTEXT,
        targets=read_component_targets(
            environment(component),
            context=CONTEXT,
        ),
    )
    return factories[component]({
        "profile": "standard",
        "kind": "lambda",
        "arn": ARN,
    })


def test_missing_configuration_creates_no_factories():
    assert read_component_targets({}, context=CONTEXT) == {}


@pytest.mark.parametrize(
    "arn",
    [
        ARN.rsplit(":", 1)[0],
        ARN.rsplit(":", 1)[0] + ":live",
        ARN.rsplit(":", 1)[0] + ":$LATEST",
        ARN.replace("111122223333", "999988887777"),
        ARN.replace("eu-west-2", "eu-west-1"),
        ARN.replace(":3", ":0"),
    ],
)
def test_target_configuration_rejects_unpinned_or_wrong_scope(arn):
    value = environment()
    value["BATCH_COMPONENT_TARGETS"] = json.dumps({
        "raw_history": [arn],
    })

    with pytest.raises(ValueError):
        read_component_targets(value, context=CONTEXT)


def test_configured_targets_require_execution_context():
    value = environment()
    value["BATCH_CONTEXT_VERSION"] = "legacy"

    with pytest.raises(ValueError, match="BATCH_CONTEXT_VERSION"):
        read_component_targets(value, context=CONTEXT)


def test_duplicate_config_keys_are_rejected():
    with pytest.raises(ValueError):
        read_component_targets(
            {
                "BATCH_CONTEXT_VERSION": "1",
                "BATCH_COMPONENT_TARGETS": (
                    '{"processing":[],"processing":[]}'
                ),
            },
            context=CONTEXT,
        )


def test_factory_rejects_different_pinned_target_before_invocation():
    client = Mock()
    factory = build_component_factories(
        client=client,
        context=CONTEXT,
        targets={"raw_history": [ARN]},
    )["raw_history"]

    with pytest.raises(ComponentTargetUnavailable):
        factory({
            "profile": "standard",
            "kind": "lambda",
            "arn": ARN[:-1] + "4",
        })

    client.invoke.assert_not_called()


@pytest.mark.parametrize("status", ["ACCEPTED", "RUNNING"])
def test_raw_history_acceptance_is_not_completion(
    context_request,
    status,
):
    _, request, _ = context_request
    client = Mock()
    client.invoke.return_value = wire(response(status))

    assert adapter(client).submit(
        operation_id=OPERATION,
        request_fingerprint=FINGERPRINT,
        request=request,
    ) is None

    call = client.invoke.call_args.kwargs
    assert call["FunctionName"] == ARN
    assert call["InvocationType"] == "RequestResponse"

    event = json.loads(call["Payload"])
    assert event["action"] == "submit_raw_history"
    assert event["request"]["raw_batch"] == request["raw_batch"]
    assert (
        event["request"]["execution_context"]
        == request["execution_context"]
    )


def test_history_lost_response_recovers_via_lookup(context_request):
    _, request, _ = context_request
    client = Mock()
    client.invoke.side_effect = [
        TimeoutError("response lost"),
        wire(response()),
    ]
    target = adapter(client)

    with pytest.raises(TimeoutError):
        target.submit(
            operation_id=OPERATION,
            request_fingerprint=FINGERPRINT,
            request=request,
        )

    completion = target.lookup(operation_id=OPERATION)

    assert completion.result_reference["version_id"] == "version-one"
    assert [
        json.loads(call.kwargs["Payload"])["action"]
        for call in client.invoke.call_args_list
    ] == [
        "submit_raw_history",
        "lookup_raw_history",
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"response_version": True},
        {"operation_id": "wrong"},
        {"status": "UNKNOWN"},
        {"request_fingerprint": "invalid"},
        {"result_reference": {}},
    ],
)
def test_invalid_history_evidence_is_rejected(change):
    client = Mock()
    client.invoke.return_value = wire({
        **response(),
        **change,
    })

    with pytest.raises(ComponentProtocolError):
        adapter(client).lookup(operation_id=OPERATION)


def test_missing_operation_is_pending_not_success():
    client = Mock()
    client.invoke.return_value = wire({
        "response_version": 1,
        "operation_id": OPERATION,
        "status": "NOT_FOUND",
    })

    assert adapter(client).lookup(operation_id=OPERATION) is None


def test_component_failure_is_not_missing_dependency():
    client = Mock()
    client.invoke.return_value = wire(response("FAILED"))

    with pytest.raises(ComponentFailed, match="ingestion-one"):
        adapter(client).lookup(operation_id=OPERATION)


def test_lambda_function_error_is_not_completion():
    client = Mock()
    client.invoke.return_value = {
        **wire({"errorMessage": "failed"}),
        "FunctionError": "Unhandled",
    }

    with pytest.raises(ComponentInvocationError):
        adapter(client).lookup(operation_id=OPERATION)


def test_processing_uses_existing_protocol(context_request):
    _, request, _ = context_request
    client = Mock()
    result = response("ACCEPTED")
    result.pop("result_reference")
    client.invoke.return_value = wire(result)

    target = adapter(client, "processing")

    assert isinstance(target, BatchProcessingAdapter)
    assert target.submit(
        operation_id=OPERATION,
        request_fingerprint=FINGERPRINT,
        request=request,
    ) is None

    assert json.loads(
        client.invoke.call_args.kwargs["Payload"]
    )["action"] == "submit_processing"


def test_bootstrap_builds_configured_factory(
    bootstrap_setup,
    monkeypatch,
):
    settings, event, lookup, _, target = bootstrap_setup
    settings.update(environment("processing"))
    target["arn"] = ARN

    stage = Mock()
    stage.advance.return_value = {
        "stage": "PROCESSING",
        "status": "RUNNING",
    }
    stage_factory = Mock(return_value=stage)
    monkeypatch.setattr(
        batch_bootstrap,
        "BatchProcessing",
        stage_factory,
    )

    session = Mock()
    handler = batch_bootstrap.build_handler(
        settings,
        session=session,
    )
    handler(event, None)

    assert isinstance(
        stage_factory.call_args.kwargs["adapter"],
        BatchProcessingAdapter,
    )

    config = next(
        call.kwargs["config"]
        for call in session.client.call_args_list
        if call.args[0] == "lambda"
    )
    assert config.retries["total_max_attempts"] == 1


def test_invalid_configuration_rejected_before_clients(bootstrap_setup):
    settings, _, _, _, _ = bootstrap_setup
    settings.update(environment())
    settings["BATCH_CONTEXT_VERSION"] = "legacy"
    session = Mock()

    with pytest.raises(ValueError):
        batch_bootstrap.build_handler(
            settings,
            session=session,
        )

    session.client.assert_not_called()