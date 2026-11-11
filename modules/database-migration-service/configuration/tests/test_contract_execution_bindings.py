import copy

import pytest

from ingestion_config.contract_bindings import (
    resolve_contract_execution_bindings,
)
from ingestion_config.loader import ConfigurationError


@pytest.fixture
def execution_bindings():
    prefix = "arn:aws:lambda:eu-west-2:123456789012:function:"

    binding = {
        "environment": "development",
        "account_id": "123456789012",
        "region": "eu-west-2",
        "configuration_revision": "a" * 64,
        "artifact_revision": "b" * 64,
        "source": "sandbox-postgres",
        "database_name": "dms_test",
        "table": {
            "schema_name": "public",
            "table_name": "example",
        },
    }

    configured = {
        "generation_function_arn": prefix + "schema-generation:7",
        "validation_function_arn": prefix + "schema-validation:9",
        "registration_function_arn": prefix + "schema-registration:5",
        "registry_table_name": "test-contract-registry",
        "registry_table_arn": (
            "arn:aws:dynamodb:eu-west-2:123456789012:"
            "table/test-contract-registry"
        ),
    }

    catalog = {
        "catalog_version": 1,
        **{
            field: binding[field]
            for field in (
                "environment",
                "account_id",
                "region",
                "configuration_revision",
                "artifact_revision",
            )
        },
        "contracts": {
            "tables": [
                {
                    "source": binding["source"],
                    "database_name": binding["database_name"],
                    **binding["table"],
                    "generation": {
                        "kind": "lambda",
                        "arn": configured["generation_function_arn"],
                    },
                    "validation": {
                        "kind": "lambda",
                        "arn": configured["validation_function_arn"],
                    },
                }
            ],
            "registration": {
                "kind": "lambda",
                "arn": configured["registration_function_arn"],
            },
            "registry": {
                "table_name": configured["registry_table_name"],
                "arn": configured["registry_table_arn"],
            },
        },
    }

    return {
        "binding": binding,
        "catalog": catalog,
        "configured": configured,
    }


def resolve(setup):
    return resolve_contract_execution_bindings(
        setup["binding"],
        setup["catalog"],
        **setup["configured"],
    )


def test_exact_execution_targets_are_resolved(execution_bindings):
    setup = execution_bindings

    result = resolve(setup)

    assert result["generation"]["arn"] == (
        setup["configured"]["generation_function_arn"]
    )
    assert result["validation"]["arn"] == (
        setup["configured"]["validation_function_arn"]
    )
    assert result["registration"]["arn"] == (
        setup["configured"]["registration_function_arn"]
    )
    assert result["registry"] == (
        setup["catalog"]["contracts"]["registry"]
    )


@pytest.mark.parametrize(
    "capability",
    ["generation", "validation", "registration"],
)
def test_different_function_in_same_account_is_rejected(
    execution_bindings,
    capability,
):
    setup = execution_bindings
    setup["configured"][f"{capability}_function_arn"] = (
        "arn:aws:lambda:eu-west-2:123456789012:"
        "function:another-function:7"
    )

    with pytest.raises(ConfigurationError, match="does not match"):
        resolve(setup)


@pytest.mark.parametrize(
    "capability",
    ["generation", "validation", "registration"],
)
def test_different_published_version_is_rejected(
    execution_bindings,
    capability,
):
    setup = execution_bindings
    field = f"{capability}_function_arn"
    setup["configured"][field] = (
        setup["configured"][field].rsplit(":", 1)[0] + ":999"
    )

    with pytest.raises(ConfigurationError, match="does not match"):
        resolve(setup)


@pytest.mark.parametrize(
    "capability",
    ["generation", "validation", "registration"],
)
@pytest.mark.parametrize("qualifier", ["", ":live"])
def test_catalog_requires_numeric_published_versions(
    execution_bindings,
    capability,
    qualifier,
):
    setup = execution_bindings

    owner = (
        setup["catalog"]["contracts"]
        if capability == "registration"
        else setup["catalog"]["contracts"]["tables"][0]
    )
    arn = owner[capability]["arn"].rsplit(":", 1)[0] + qualifier

    owner[capability]["arn"] = arn
    setup["configured"][f"{capability}_function_arn"] = arn

    with pytest.raises(ConfigurationError, match="numeric"):
        resolve(setup)


@pytest.mark.parametrize(
    "field",
    ["registry_table_name", "registry_table_arn"],
)
def test_different_registry_target_is_rejected(
    execution_bindings,
    field,
):
    setup = execution_bindings
    setup["configured"][field] = (
        "another-contract-registry"
        if field == "registry_table_name"
        else (
            "arn:aws:dynamodb:eu-west-2:123456789012:"
            "table/another-contract-registry"
        )
    )

    with pytest.raises(ConfigurationError, match="does not match"):
        resolve(setup)


def test_catalog_must_match_pinned_artifact_revision(
    execution_bindings,
):
    setup = execution_bindings
    setup["catalog"]["artifact_revision"] = "c" * 64

    with pytest.raises(ConfigurationError, match="artifact_revision"):
        resolve(setup)


def test_other_table_capabilities_cannot_be_selected(
    execution_bindings,
):
    setup = execution_bindings
    setup["catalog"]["contracts"]["tables"][0]["table_name"] = (
        "another_table"
    )

    with pytest.raises(ConfigurationError, match="exactly one"):
        resolve(setup)


def test_resolution_does_not_mutate_inputs(execution_bindings):
    setup = execution_bindings
    original = copy.deepcopy(setup)

    result = resolve(setup)
    result["generation"]["arn"] = "changed-after-resolution"

    assert setup == original