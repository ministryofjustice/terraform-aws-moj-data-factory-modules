from unittest.mock import Mock

import pytest

from dms_runtime import batch_bootstrap, batch_lambda_function
from ingestion_registry.ownership import OwnershipLost

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

def test_bootstrap_dispatches_configured_reconciliation(bootstrap_setup):
    environment, event, lookup, progress, _ = bootstrap_setup
    environment = {**environment, "BATCH_RECONCILIATION_REQUIRED": "true"}
    event = {**event, "action": "advance_reconciliation"}

    reconciliation = Mock()
    reconciliation.advance.return_value = {
        "stage": "RECONCILIATION",
        "status": "RUNNING",
    }
    factory = Mock(return_value=reconciliation)

    handler = batch_bootstrap.build_handler(
        environment,
        session=Mock(),
        reconciliation_factory=factory,
    )

    assert handler(event, None) == {
        "ingestion_id": event["ingestion_id"],
        "stage": "RECONCILIATION",
        "status": "RUNNING",
    }
    factory.assert_called_once()
    assert factory.call_args.args[1] is progress
    lookup.resolve.assert_called_once_with(event["ingestion_id"])
    reconciliation.advance.assert_called_once()
    assert reconciliation.advance.call_args.args[0].ingestion_id == (
        event["ingestion_id"]
    )

@pytest.mark.parametrize(
    ("action", "stage", "factory_name"),
    [
        ("advance_cdc_readiness", "CDC_CONSUMER", "cdc_readiness_factory"),
        ("advance_cdc_drain", "CDC_DRAINED", "cdc_drain_factory"),
    ],
)
def test_bootstrap_dispatches_only_the_requested_cdc_operation(
    bootstrap_setup,
    action,
    stage,
    factory_name,
):
    environment, event, lookup, progress, _ = bootstrap_setup
    event = {**event, "action": action}
    operation = Mock()
    operation.advance.return_value = {"stage": stage, "status": "RUNNING"}
    factory = Mock(return_value=operation)

    handler = batch_bootstrap.build_handler(
        environment,
        session=Mock(),
        **{factory_name: factory},
    )

    assert handler(event, None) == {
        "ingestion_id": event["ingestion_id"],
        "stage": stage,
        "status": "RUNNING",
    }
    factory.assert_called_once()
    assert factory.call_args.args[-1] is progress
    lookup.resolve.assert_called_once_with(event["ingestion_id"])
    operation.advance.assert_called_once()
    assert operation.advance.call_args.args[0].ingestion_id == (
        event["ingestion_id"]
    )


@pytest.mark.parametrize(
    "action",
    ["advance_cdc_readiness", "advance_cdc_drain"],
)
def test_bootstrap_rejects_wrong_owner_before_cdc_dispatch(
    bootstrap_setup,
    action,
):
    environment, event, lookup, _, _ = bootstrap_setup
    event = {
        **event,
        "action": action,
        "ingestion_owner": {
            **event["ingestion_owner"],
            "fencing_token": event["ingestion_owner"]["fencing_token"] + 1,
        },
    }
    readiness = Mock()
    drain = Mock()
    handler = batch_bootstrap.build_handler(
        environment,
        session=Mock(),
        cdc_readiness_factory=Mock(return_value=readiness),
        cdc_drain_factory=Mock(return_value=drain),
    )

    with pytest.raises(OwnershipLost, match="expired or superseded"):
        handler(event, None)

    lookup.resolve.assert_not_called()
    readiness.advance.assert_not_called()
    drain.advance.assert_not_called()