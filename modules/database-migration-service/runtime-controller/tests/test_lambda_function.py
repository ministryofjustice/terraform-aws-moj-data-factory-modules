import json
from decimal import Decimal
from unittest.mock import Mock

import pytest

from dms_runtime import lambda_function as entrypoint


@pytest.fixture
def deployment_settings():
    return {
        "RUNTIME_ENVIRONMENT": "development",
        "RUNTIME_ACCOUNT_ID": "111122223333",
        "AWS_REGION": "eu-west-2",
        "RUNTIME_SOURCE": "sandbox-postgres",
        "RUNTIME_DATABASE_NAME": "dms_test",
        "INGESTION_REGISTRY_TABLE_NAME": "runtime-registry",
        "PUBLISHED_DOCUMENT_BUCKET": "example-config",
        "PUBLISHED_DOCUMENT_PREFIX": "deployments/",
        "PUBLISHED_DOCUMENT_BUCKET_OWNER": "111122223333",
        "SOURCE_CREDENTIAL_MODE": "synchronise_then_test",
        "CREDENTIAL_SYNC_FUNCTION_ARN": (
            "arn:aws:lambda:eu-west-2:"
            "111122223333:function:source-credential-sync"
        ),
    }


def test_factory_constructs_shared_runtime_and_single_attempt_clients(
    deployment_settings,
):
    session = Mock()

    handler = entrypoint.build_handler(
        deployment_settings,
        session=session,
    )

    calls = {
        call.args[0]: call.kwargs["config"]
        for call in session.client.call_args_list
    }

    assert set(calls) == {
        "dynamodb",
        "s3",
        "dms",
        "lambda",
    }

    assert calls["dms"].region_name == "eu-west-2"
    assert calls["dms"].retries["total_max_attempts"] == 1
    assert calls["lambda"].retries["total_max_attempts"] == 1

    assert handler.lookup.context.source == "sandbox-postgres"
    assert handler.lookup.allowed_bucket == "example-config"
    assert handler.lookup.allowed_prefix == "deployments/"
    assert handler.lookup.registry.table_name == "runtime-registry"

    owned = handler.owned_action_handler

    assert owned.guard_factory.registry is handler.lookup.registry
    assert owned.guard_factory.context == handler.lookup.context
    assert owned.credential_sync_function_arn == (
        deployment_settings["CREDENTIAL_SYNC_FUNCTION_ARN"]
    )
    assert handler.full_load.inspection is handler.inspection


def test_connection_only_mode_does_not_configure_credential_sync(
    deployment_settings,
):
    deployment_settings["RUNTIME_SOURCE"] = "sandbox-oracle"
    deployment_settings["RUNTIME_DATABASE_NAME"] = "DMSTEST"
    deployment_settings["SOURCE_CREDENTIAL_MODE"] = "connection_test"
    del deployment_settings["CREDENTIAL_SYNC_FUNCTION_ARN"]

    handler = entrypoint.build_handler(
        deployment_settings,
        session=Mock(),
    )

    assert handler.lookup.context.source == "sandbox-oracle"
    assert (
        handler.owned_action_handler.credential_sync_function_arn
        is None
    )


@pytest.mark.parametrize(
    "setting",
    [
        "RUNTIME_SOURCE",
        "INGESTION_REGISTRY_TABLE_NAME",
        "PUBLISHED_DOCUMENT_PREFIX",
        "SOURCE_CREDENTIAL_MODE",
        "CREDENTIAL_SYNC_FUNCTION_ARN",
    ],
)
def test_required_deployment_settings_cannot_be_omitted(
    deployment_settings,
    setting,
):
    del deployment_settings[setting]
    session = Mock()

    with pytest.raises(ValueError, match=setting):
        entrypoint.build_handler(
            deployment_settings,
            session=session,
        )

    session.client.assert_not_called()


@pytest.mark.parametrize(
    "prefix",
    [
        "/deployments/",
        "deployments",
        "deployments/../",
        "deployments//",
    ],
)
def test_invalid_document_prefix_is_rejected_before_client_creation(
    deployment_settings,
    prefix,
):
    deployment_settings["PUBLISHED_DOCUMENT_PREFIX"] = prefix
    session = Mock()

    with pytest.raises(ValueError, match="PUBLISHED_DOCUMENT_PREFIX"):
        entrypoint.build_handler(
            deployment_settings,
            session=session,
        )

    session.client.assert_not_called()


def test_connection_only_mode_rejects_conflicting_sync_setting(
    deployment_settings,
):
    deployment_settings["SOURCE_CREDENTIAL_MODE"] = "connection_test"

    with pytest.raises(
        ValueError,
        match="must omit CREDENTIAL_SYNC_FUNCTION_ARN",
    ):
        entrypoint.build_handler(
            deployment_settings,
            session=Mock(),
        )


def test_unknown_credential_mode_is_rejected(
    deployment_settings,
):
    deployment_settings["SOURCE_CREDENTIAL_MODE"] = "automatic"

    with pytest.raises(ValueError, match="SOURCE_CREDENTIAL_MODE"):
        entrypoint.build_handler(
            deployment_settings,
            session=Mock(),
        )


def test_entrypoint_returns_json_safe_saved_results(monkeypatch):
    handler = Mock()
    handler.handle.return_value = {
        "outcome": "REQUESTED",
        "preflight": {
            "pollAttempts": Decimal("2"),
        },
    }

    monkeypatch.setattr(
        entrypoint,
        "_get_handler",
        Mock(return_value=handler),
    )

    event = {
        "request_version": 1,
        "action": "task_status",
        "ingestion_id": "example-ingestion",
    }

    result = entrypoint.lambda_handler(event, None)

    handler.handle.assert_called_once_with(event)
    assert type(result["preflight"]["pollAttempts"]) is int
    json.dumps(result)


def test_entrypoint_does_not_hide_runtime_failures(monkeypatch):
    handler = Mock()
    handler.handle.side_effect = RuntimeError("Ownership lost")

    monkeypatch.setattr(
        entrypoint,
        "_get_handler",
        Mock(return_value=handler),
    )

    with pytest.raises(RuntimeError, match="Ownership lost"):
        entrypoint.lambda_handler({}, None)