from unittest.mock import Mock

import pytest

from dms_runtime import batch_bootstrap, batch_lambda_function

@pytest.fixture
def bootstrap_setup(monkeypatch):
    environment = {
        "RUNTIME_ENVIRONMENT": "development",
        "RUNTIME_ACCOUNT_ID": "111122223333",
        "AWS_REGION": "eu-west-2",
        "RUNTIME_SOURCE": "sandbox-postgres",
        "RUNTIME_DATABASE_NAME": "dms_test",
        "BATCH_RECONCILIATION_REQUIRED": "false",
        "INGESTION_REGISTRY_TABLE_NAME": "runtime-registry",
        "PUBLISHED_DOCUMENT_BUCKET": "example-config",
        "PUBLISHED_DOCUMENT_PREFIX": "deployments/",
        "PUBLISHED_DOCUMENT_BUCKET_OWNER": "111122223333",
    }
    registry = Mock()
    registry._read.return_value = {
        "execution_id": "table-worker",
        "fencing_token": 1,
        "attempt": 1,
        "lease_expires_at": 1300,
        "recovery_required": False,
    }
    target = {
        "profile": "batch",
        "kind": "lambda",
        "arn": "arn:aws:lambda:eu-west-2:111122223333:function:processing",
    }
    lookup = Mock()
    lookup.resolve.return_value = {
        "component_bindings": {"processing": target},
    }
    progress = Mock()
    progress.read_stage.return_value = None

    monkeypatch.setattr(
        batch_bootstrap, "IngestionRegistry", Mock(return_value=registry)
    )
    monkeypatch.setattr(
        batch_bootstrap, "PinnedRuntimeLookup", Mock(return_value=lookup)
    )
    monkeypatch.setattr(
        batch_bootstrap, "IngestionProgress", Mock(return_value=progress)
    )
    monkeypatch.setattr(batch_lambda_function.time, "time", lambda: 1000)

    event = {
        "request_version": 1,
        "action": "advance_processing",
        "ingestion_id": "ingestion-one",
        "ingestion_owner": {
            "execution_id": "table-worker",
            "fencing_token": 1,
            "attempt": 1,
        },
    }
    return environment, event, lookup, progress, target


def test_bootstrap_passes_pinned_processing_target_to_adapter_factory(
    bootstrap_setup, monkeypatch
):
    environment, event, lookup, progress, target = bootstrap_setup
    adapter = Mock()
    adapter_factory = Mock(return_value=adapter)
    processing = Mock()
    processing.advance.return_value = {
        "stage": "PROCESSING",
        "status": "RUNNING",
    }
    stage_factory = Mock(return_value=processing)
    monkeypatch.setattr(batch_bootstrap, "BatchProcessing", stage_factory)

    handler = batch_bootstrap.build_handler(
        environment,
        session=Mock(),
        adapter_factories={"processing": adapter_factory},
    )

    assert handler(event, None) == {
        "ingestion_id": "ingestion-one",
        "stage": "PROCESSING",
        "status": "RUNNING",
    }
    lookup.resolve.assert_called_with("ingestion-one")
    adapter_factory.assert_called_once_with(target)
    assert stage_factory.call_args.kwargs["adapter"] is adapter
    assert stage_factory.call_args.kwargs["progress"] is progress
    claim = processing.advance.call_args.args[0]
    assert claim.ingestion_id == "ingestion-one"
    assert claim.lease_expires_at == 1300


def test_bootstrap_rejects_missing_adapter_before_reserving_stage(
    bootstrap_setup,
):
    environment, event, lookup, progress, _ = bootstrap_setup
    handler = batch_bootstrap.build_handler(environment, session=Mock())

    with pytest.raises(
        batch_bootstrap.BatchComponentUnavailable,
        match="processing has no configured adapter",
    ):
        handler(event, None)

    lookup.resolve.assert_called_once_with("ingestion-one")
    progress.record_stage.assert_not_called()