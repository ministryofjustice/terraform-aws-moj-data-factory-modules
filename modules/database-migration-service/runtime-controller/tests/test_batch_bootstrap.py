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

@pytest.mark.parametrize("version", ["2", "", "v1"])
def test_bootstrap_rejects_unknown_context_version(
    bootstrap_setup,
    version,
):
    environment, _, _, progress, _ = bootstrap_setup
    environment = {
        **environment,
        "BATCH_CONTEXT_VERSION": version,
    }

    with pytest.raises(
        ValueError,
        match="BATCH_CONTEXT_VERSION",
    ):
        batch_bootstrap.build_handler(
            environment,
            session=Mock(),
        )

    progress.record_stage.assert_not_called()


@pytest.mark.parametrize("version", [None, "legacy", "1"])
def test_bootstrap_selects_context_reader(
    bootstrap_setup,
    monkeypatch,
    version,
):
    environment, event, lookup, _, _ = bootstrap_setup
    environment = dict(environment)

    if version is not None:
        environment["BATCH_CONTEXT_VERSION"] = version

    reader = Mock(return_value={"context_version": 1})
    monkeypatch.setattr(
        batch_bootstrap,
        "read_downstream_context",
        reader,
    )

    processing = Mock()
    processing.advance.return_value = {
        "stage": "PROCESSING",
        "status": "RUNNING",
    }
    factory = Mock(return_value=processing)
    monkeypatch.setattr(
        batch_bootstrap,
        "BatchProcessing",
        factory,
    )

    handler = batch_bootstrap.build_handler(
        environment,
        session=Mock(),
        adapter_factories={"processing": Mock()},
    )
    handler(event, None)

    downstream = factory.call_args.kwargs["downstream"]
    result = downstream.execution_context("ingestion-one")

    if version == "1":
        assert result == {"context_version": 1}
        reader.assert_called_once_with(
            registry=batch_bootstrap.IngestionRegistry.return_value,
            lookup=lookup,
            ingestion_id="ingestion-one",
        )
    else:
        assert result is None
        reader.assert_not_called()

@pytest.mark.parametrize(
    ("component", "action", "class_name", "stage"),
    [
        (
            "processing",
            "advance_processing",
            "BatchProcessing",
            "PROCESSING",
        ),
        (
            "raw_history",
            "advance_raw_history",
            "BatchRawHistory",
            "HISTORY_BASELINE",
        ),
        (
            "publication",
            "advance_publication",
            "BatchPublication",
            "PUBLICATION",
        ),
    ],
)
def test_context_v1_uses_component_lookup(
    bootstrap_setup,
    monkeypatch,
    component,
    action,
    class_name,
    stage,
):
    environment, event, base, progress, _ = bootstrap_setup
    environment = {
        **environment,
        "BATCH_CONTEXT_VERSION": "1",
    }

    target = {
        "profile": "standard",
        "kind": "lambda",
        "arn": (
            "arn:aws:lambda:eu-west-2:111122223333:"
            f"function:{component}:3"
        ),
    }
    deployment = {
        "component_bindings": {
            component: target,
        },
        "downstream_binding": {
            "component": component,
            "binding_sha256": "d" * 64,
        },
    }

    selected = Mock()
    selected.resolve.return_value = deployment
    lookups = {}

    def component_lookup(
        base_lookup,
        *,
        component,
        registry,
    ):
        assert base_lookup is base

        selected_component = deployment[
            "downstream_binding"
        ]["component"]

        lookups[component] = (
            selected
            if component == selected_component
            else Mock()
        )
        return lookups[component]

    monkeypatch.setattr(
        batch_bootstrap,
        "DownstreamComponentLookup",
        component_lookup,
    )

    operation = Mock()
    operation.advance.return_value = {
        "stage": stage,
        "status": "RUNNING",
    }
    stage_factory = Mock(return_value=operation)

    monkeypatch.setattr(
        batch_bootstrap,
        class_name,
        stage_factory,
    )

    adapter_factory = Mock()

    handler = batch_bootstrap.build_handler(
        environment,
        session=Mock(),
        adapter_factories={
            component: adapter_factory,
        },
    )

    result = handler(
        {**event, "action": action},
        None,
    )

    assert result["status"] == "RUNNING"
    assert set(lookups) == {
        "processing",
        "raw_history",
        "publication",
    }

    selected.resolve.assert_called_once_with(
        event["ingestion_id"]
    )
    adapter_factory.assert_called_once_with(target)

    downstream = stage_factory.call_args.kwargs["downstream"]

    assert downstream._resolve_deployment(
        event["ingestion_id"]
    ) == deployment

    base.resolve.assert_called_once_with(
        event["ingestion_id"]
    )


def test_legacy_bootstrap_keeps_original_lookup(
    bootstrap_setup,
    monkeypatch,
):
    environment, _, _, _, _ = bootstrap_setup
    factory = Mock()

    monkeypatch.setattr(
        batch_bootstrap,
        "DownstreamComponentLookup",
        factory,
    )

    batch_bootstrap.build_handler(
        environment,
        session=Mock(),
    )

    factory.assert_not_called()

@pytest.mark.parametrize(
    "context_version",
    ["legacy", "1"],
)
def test_bootstrap_routes_explicit_component_pin(
    bootstrap_setup,
    monkeypatch,
    context_version,
):
    environment, _, lookup, _, _ = bootstrap_setup

    environment = {
        **environment,
        "BATCH_CONTEXT_VERSION": context_version,
    }

    selected = Mock(
        return_value={"status": "PINNED"}
    )
    factory = Mock(return_value=selected)

    monkeypatch.setattr(
        batch_bootstrap,
        "build_component_binding_entry_point",
        factory,
    )

    handler = batch_bootstrap.build_handler(
        environment,
        session=Mock(),
    )

    event = {
        "action": "pin_downstream_component",
    }

    if context_version == "1":
        assert handler(event, None) == {
            "status": "PINNED",
        }

        selected.assert_called_once_with(event, None)

        assert factory.call_args.kwargs["lookup"] is lookup

    else:
        with pytest.raises(
            ValueError,
            match="BATCH_CONTEXT_VERSION=1",
        ):
            handler(event, None)

        selected.assert_not_called()
