import copy
import hashlib
import json
from dataclasses import asdict
from unittest.mock import Mock

import pytest

from dms_runtime import lookup as lookup_module
from dms_runtime.lookup import PinnedRuntimeLookup
from dms_runtime.ownership import (
    RuntimeContext,
    RuntimeScopeDenied,
)


@pytest.fixture
def lookup_setup(monkeypatch):
    context = RuntimeContext(
        environment="development",
        account_id="111122223333",
        region="eu-west-2",
        source="sandbox-postgres",
        database_name="dms_test",
    )

    reference = {
        "bucket_name": "example-config",
        "object_key": "deployments/manifest.json",
        "version_id": "manifest-one",
        "sha256": "c" * 64,
    }

    binding = {
        **asdict(context),
        "configuration_revision": "a" * 64,
        "artifact_revision": "b" * 64,
        "extraction_generation": "generation-one",
        "table": {
            "schema_name": "public",
            "table_name": "example",
        },
        "document_references": {
            "manifest": reference,
            "artifact": {
                **reference,
                "object_key": "deployments/artifact.json",
                "version_id": "artifact-one",
            },
        },
    }

    metadata = {
        "table_identity": {
            "environment": context.environment,
            "source": context.source,
            "database_name": context.database_name,
            "schema_name": "public",
            "table_name": "example",
        },
        "configuration_revision": "a" * 64,
        "artifact_revision": "b" * 64,
        "extraction_generation": "generation-one",
    }

    def pinned_record():
        encoded = json.dumps(
            binding,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")

        return {
            "binding": binding,
            "binding_fingerprint": hashlib.sha256(
                encoded
            ).hexdigest(),
        }

    registry = Mock()
    registry._read.side_effect = (
        lambda pk, sk: (
            metadata
            if sk == "META"
            else pinned_record()
        )
    )

    resolver = Mock(
        side_effect=lambda *args, **kwargs: copy.deepcopy(binding)
    )
    monkeypatch.setattr(
        lookup_module,
        "resolve_published_table",
        resolver,
    )

    document_client = Mock()

    lookup = PinnedRuntimeLookup(
        registry,
        document_client,
        context=context,
        allowed_bucket="example-config",
        allowed_prefix="deployments/",
        expected_bucket_owner="111122223333",
    )

    return (
        registry,
        metadata,
        binding,
        resolver,
        document_client,
        lookup,
    )


def test_lookup_uses_original_registry_references(lookup_setup):
    registry, _, binding, resolver, document_client, lookup = (
        lookup_setup
    )

    result = lookup.resolve("ingestion-one")

    assert result == binding
    assert resolver.call_args.args == (document_client,)

    kwargs = resolver.call_args.kwargs

    assert kwargs["manifest_reference"].version_id == "manifest-one"
    assert kwargs["artifact_reference"].version_id == "artifact-one"
    assert kwargs["lookup"].configuration_revision == "a" * 64
    assert kwargs["lookup"].artifact_revision == "b" * 64
    assert kwargs["lookup"].source == "sandbox-postgres"
    assert kwargs["allowed_bucket"] == "example-config"
    assert kwargs["allowed_prefix"] == "deployments/"
    assert kwargs["expected_bucket_owner"] == "111122223333"

    assert registry._read.call_args_list[0].args == (
        "INGESTION#ingestion-one",
        "META",
    )


def test_unknown_ingestion_is_rejected_before_document_lookup(
    lookup_setup,
):
    registry, _, _, resolver, _, lookup = lookup_setup

    registry._read.side_effect = None
    registry._read.return_value = None

    with pytest.raises(RuntimeScopeDenied, match="does not exist"):
        lookup.resolve("unknown")

    resolver.assert_not_called()


def test_unbound_ingestion_is_rejected(lookup_setup):
    registry, metadata, _, resolver, _, lookup = lookup_setup
    registry._read.side_effect = [metadata, None]

    with pytest.raises(RuntimeScopeDenied, match="no pinned"):
        lookup.resolve("ingestion-one")

    resolver.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("environment", "test"),
        ("source", "another-source"),
        ("database_name", "another_db"),
    ],
)
def test_another_ingestion_service_is_rejected(
    lookup_setup,
    field,
    value,
):
    _, metadata, _, resolver, _, lookup = lookup_setup
    metadata["table_identity"][field] = value

    with pytest.raises(
        RuntimeScopeDenied,
        match="authorised service",
    ):
        lookup.resolve("ingestion-one")

    resolver.assert_not_called()


@pytest.mark.parametrize(
    "field",
    [
        "configuration_revision",
        "artifact_revision",
        "extraction_generation",
    ],
)
def test_revision_mismatch_is_rejected(lookup_setup, field):
    _, metadata, _, resolver, _, lookup = lookup_setup
    metadata[field] = "another-revision"

    with pytest.raises(RuntimeScopeDenied, match="revisions"):
        lookup.resolve("ingestion-one")

    resolver.assert_not_called()


def test_resolved_resource_change_is_rejected(lookup_setup):
    _, _, binding, resolver, _, lookup = lookup_setup

    resolver.side_effect = None
    resolver.return_value = {
        **binding,
        "resources": {
            "replication_task_arn": "another-task",
        },
    }

    with pytest.raises(RuntimeScopeDenied, match="pinned binding"):
        lookup.resolve("ingestion-one")


def test_catalog_and_capability_plan_are_reused(lookup_setup):
    _, _, binding, resolver, _, lookup = lookup_setup

    binding["document_references"]["catalog"] = {
        **binding["document_references"]["manifest"],
        "object_key": "deployments/catalog.json",
        "version_id": "catalog-one",
    }
    binding["component_bindings"] = {
        "processing": {},
        "raw_history": {},
    }
    binding["contract_bindings"] = {
        "generation": {},
    }

    lookup.resolve("ingestion-one")
    kwargs = resolver.call_args.kwargs

    assert kwargs["catalog_reference"].version_id == "catalog-one"
    assert kwargs["required_components"] == (
        "processing",
        "raw_history",
    )
    assert kwargs["required_contract_capabilities"] == (
        "generation",
    )


def test_document_failure_does_not_fall_back_to_other_references(
    lookup_setup,
):
    _, _, _, resolver, _, lookup = lookup_setup
    resolver.side_effect = RuntimeError(
        "Pinned document unavailable"
    )

    with pytest.raises(
        RuntimeError,
        match="Pinned document unavailable",
    ):
        lookup.resolve("ingestion-one")

    assert resolver.call_count == 1


@pytest.mark.parametrize(
    "value",
    ["", " ingestion-one", None],
)
def test_invalid_ingestion_identity_is_rejected(
    lookup_setup,
    value,
):
    registry, _, _, resolver, _, lookup = lookup_setup

    with pytest.raises(ValueError, match="ingestion_id"):
        lookup.resolve(value)

    registry._read.assert_not_called()
    resolver.assert_not_called()


def test_table_mismatch_is_rejected_before_document_lookup(
    lookup_setup,
):
    _, _, binding, resolver, _, lookup = lookup_setup
    binding["table"]["table_name"] = "another_table"

    with pytest.raises(
        RuntimeScopeDenied,
        match="ingestion table",
    ):
        lookup.resolve("ingestion-one")

    resolver.assert_not_called()


@pytest.mark.parametrize("reference", ["manifest", "artifact"])
def test_required_document_reference_cannot_be_omitted(
    lookup_setup,
    reference,
):
    _, _, binding, resolver, _, lookup = lookup_setup
    del binding["document_references"][reference]

    with pytest.raises(RuntimeScopeDenied, match="incomplete"):
        lookup.resolve("ingestion-one")

    resolver.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_id", "999900001111"),
        ("region", "eu-west-1"),
    ],
)
def test_pinned_binding_cannot_change_account_or_region(
    lookup_setup,
    field,
    value,
):
    _, _, binding, resolver, _, lookup = lookup_setup
    binding[field] = value

    with pytest.raises(
        RuntimeScopeDenied,
        match="authorised service",
    ):
        lookup.resolve("ingestion-one")

    resolver.assert_not_called()