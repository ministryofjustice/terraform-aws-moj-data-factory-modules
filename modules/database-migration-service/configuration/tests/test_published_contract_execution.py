import copy
from dataclasses import dataclass
from unittest.mock import Mock

import pytest

from ingestion_config import published_resolver
from ingestion_config.loader import ConfigurationError
from ingestion_config.published_resolver import TableLookup

from test_contract_execution_bindings import (
    execution_bindings as execution_bindings,
)


@dataclass(frozen=True)
class Reference:
    bucket_name: str
    object_key: str
    version_id: str
    sha256: str


@pytest.fixture
def published_execution(execution_bindings, monkeypatch):
    setup = execution_bindings
    binding = setup["binding"]

    references = {
        name: Reference(
            bucket_name="test-deployment-documents",
            object_key=f"services/sandbox-postgres/{name}/document.json",
            version_id=f"{name}-version-one",
            sha256=character * 64,
        )
        for name, character in (
            ("manifest", "a"),
            ("artifact", "b"),
            ("catalog", "c"),
        )
    }

    documents = {
        references["manifest"]: {"document": "manifest"},
        references["artifact"]: {"document": "artifact"},
        references["catalog"]: setup["catalog"],
    }

    reader = Mock(
        side_effect=lambda client, reference, **kwargs: copy.deepcopy(
            documents[reference]
        )
    )
    monkeypatch.setattr(
        published_resolver,
        "load_published_document",
        reader,
    )
    monkeypatch.setattr(
        published_resolver,
        "resolve_table_binding",
        Mock(return_value=copy.deepcopy(binding)),
    )
    monkeypatch.setattr(
        published_resolver,
        "resolve_component_bindings",
        Mock(return_value={}),
    )

    lookup = TableLookup(
        environment=binding["environment"],
        account_id=binding["account_id"],
        region=binding["region"],
        source=binding["source"],
        database_name=binding["database_name"],
        schema_name=binding["table"]["schema_name"],
        table_name=binding["table"]["table_name"],
        configuration_revision=binding["configuration_revision"],
        artifact_revision=binding["artifact_revision"],
    )

    return {
        **setup,
        "reader": reader,
        "client": Mock(),
        "references": references,
        "lookup": lookup,
    }


def resolve(setup, **changes):
    arguments = {
        "manifest_reference": setup["references"]["manifest"],
        "artifact_reference": setup["references"]["artifact"],
        "catalog_reference": setup["references"]["catalog"],
        "allowed_bucket": "test-deployment-documents",
        "allowed_prefix": "services/sandbox-postgres/",
        "expected_bucket_owner": "123456789012",
        "lookup": setup["lookup"],
        "contract_execution_targets": setup["configured"],
    }
    arguments.update(changes)

    return published_resolver.resolve_published_table(
        setup["client"],
        **arguments,
    )


def test_published_resolution_checks_all_execution_targets(
    published_execution,
):
    setup = published_execution

    result = resolve(setup)

    assert set(result["contract_bindings"]) == {
        "generation",
        "validation",
        "registration",
        "registry",
    }
    assert result["contract_bindings"]["validation"]["arn"] == (
        setup["configured"]["validation_function_arn"]
    )
    assert result["document_references"]["catalog"]["version_id"] == (
        "catalog-version-one"
    )

    assert setup["reader"].call_count == 3

    for call in setup["reader"].call_args_list:
        assert call.kwargs == {
            "allowed_bucket": "test-deployment-documents",
            "allowed_prefix": "services/sandbox-postgres/",
            "expected_bucket_owner": "123456789012",
        }


def test_execution_targets_require_catalog_before_document_reads(
    published_execution,
):
    setup = published_execution

    with pytest.raises(ConfigurationError, match="pinned component catalog"):
        resolve(setup, catalog_reference=None)

    setup["reader"].assert_not_called()


@pytest.mark.parametrize(
    "capability",
    ["generation", "validation", "registration"],
)
def test_same_account_wrong_function_blocks_published_resolution(
    published_execution,
    capability,
):
    setup = published_execution
    targets = {
        **setup["configured"],
        f"{capability}_function_arn": (
            "arn:aws:lambda:eu-west-2:123456789012:"
            "function:wrong-function:7"
        ),
    }

    with pytest.raises(ConfigurationError, match="does not match"):
        resolve(setup, contract_execution_targets=targets)


def test_wrong_registry_blocks_published_resolution(published_execution):
    setup = published_execution
    targets = {
        **setup["configured"],
        "registry_table_name": "another-registry",
    }

    with pytest.raises(ConfigurationError, match="does not match"):
        resolve(setup, contract_execution_targets=targets)


@pytest.mark.parametrize(
    "targets",
    [
        {},
        {"generation_function_arn": "some-function"},
        "invalid",
    ],
)
def test_incomplete_targets_are_rejected_before_reads(
    published_execution,
    targets,
):
    setup = published_execution

    with pytest.raises(ConfigurationError, match="exactly"):
        resolve(setup, contract_execution_targets=targets)

    setup["reader"].assert_not_called()