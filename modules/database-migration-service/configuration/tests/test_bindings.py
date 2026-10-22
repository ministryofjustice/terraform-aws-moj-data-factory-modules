import copy
from pathlib import Path
from typing import Any

import pytest

from ingestion_config.bindings import (
    validate_deployment_against_artifact,
)
from ingestion_config.deployment_inventory import (
    validate_deployment_inventory,
)
from ingestion_config.component_bindings import (
    resolve_component_bindings,
)
from ingestion_config.contract_bindings import (
    resolve_contract_bindings,
)
from ingestion_config.compiler import compile_service
from ingestion_config.loader import ConfigurationError
from ingestion_config.compiler import fingerprint
from ingestion_config.resolver import resolve_table_binding
from ingestion_config.mappings import build_table_mappings


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture
def compiled() -> dict[str, Any]:
    return compile_service(
        EXAMPLES / "postgres.yaml",
        EXAMPLES / "platform-development.yaml",
        extraction_generation="generation-one",
    )


@pytest.fixture
def deployment(
    compiled: dict[str, Any],
) -> dict[str, Any]:
    runtime = compiled["runtime_configuration"]
    group = runtime["task_groups"]["integration"]
    arn_prefix = "arn:aws:dms:eu-west-2:111122223333"

    return {
        "manifest_version": 1,
        "configuration_revision": compiled["configuration_revision"],
        "artifact_revision": compiled["artifact_revision"],
        "environment": runtime["environment"],
        "account_id": "111122223333",
        "region": runtime["region"],
        "source": runtime["source"],
        "database_name": (
            runtime["source_endpoint"]["database_name"]
        ),
        "extraction_generation": "generation-one",
        "task_groups": {
            "integration": {
                "deployment_name": group["deployment_name"],
                "migration_type": group["mode"],
                "replication_instance_arn": (
                    f"{arn_prefix}:rep:EXAMPLEINSTANCE"
                ),
                "source_endpoint_arn": (
                    f"{arn_prefix}:endpoint:EXAMPLESOURCE"
                ),
                "target_endpoint_arn": (
                    f"{arn_prefix}:endpoint:EXAMPLETARGET"
                ),
                "replication_task_arn": (
                    f"{arn_prefix}:task:EXAMPLETASK"
                ),
                "replication_task_id": group["replication_task_id"],
                "tables": [
                    {
                        "schema_name": table["schema_name"],
                        "table_name": table["table_name"],
                    }
                    for table in group["tables"]
                ],
                "landing": copy.deepcopy(group["landing"]),
            }
        },
    }


def test_matching_deployment_is_accepted(
    compiled: dict[str, Any],
    deployment: dict[str, Any],
) -> None:
    validate_deployment_against_artifact(deployment, compiled)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("configuration_revision", "c" * 64),
        ("artifact_revision", "d" * 64),
        ("environment", "test"),
        ("region", "eu-west-1"),
        ("source", "another-service"),
        ("database_name", "another_database"),
        ("extraction_generation", "generation-two"),
    ],
)
def test_different_deployment_identity_is_rejected(
    compiled: dict[str, Any],
    deployment: dict[str, Any],
    field: str,
    replacement: str,
) -> None:
    deployment[field] = replacement

    with pytest.raises(ConfigurationError, match=field):
        validate_deployment_against_artifact(deployment, compiled)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("deployment_name", "another-deployment"),
        ("migration_type", "full-load"),
        ("replication_task_id", "another-task"),
        (
            "landing",
            {
                "bucket_name": "another-raw-bucket",
                "prefix": "another-generation/",
            },
        ),
        (
            "tables",
            [
                {
                    "schema_name": "public",
                    "table_name": "unselected_table",
                }
            ],
        ),
    ],
)
def test_different_task_group_binding_is_rejected(
    compiled: dict[str, Any],
    deployment: dict[str, Any],
    field: str,
    replacement: Any,
) -> None:
    deployment["task_groups"]["integration"][field] = replacement

    with pytest.raises(ConfigurationError, match=field):
        validate_deployment_against_artifact(deployment, compiled)


def test_missing_group_is_rejected(
    compiled: dict[str, Any],
    deployment: dict[str, Any],
) -> None:
    deployment["task_groups"] = {}

    with pytest.raises(ConfigurationError, match="task_groups"):
        validate_deployment_against_artifact(deployment, compiled)


def test_modified_artifact_is_rejected(
    compiled: dict[str, Any],
    deployment: dict[str, Any],
) -> None:
    compiled["runtime_configuration"]["source"] = "another-service"

    with pytest.raises(
        ConfigurationError,
        match="artifact.artifact_revision",
    ):
        validate_deployment_against_artifact(deployment, compiled)


def test_comparison_does_not_modify_inputs(
    compiled: dict[str, Any],
    deployment: dict[str, Any],
) -> None:
    original_compiled = copy.deepcopy(compiled)
    original_deployment = copy.deepcopy(deployment)

    validate_deployment_against_artifact(deployment, compiled)

    assert compiled == original_compiled
    assert deployment == original_deployment


def test_replacement_generation_rejects_previous_manifest(
    deployment: dict[str, Any],
) -> None:
    replacement = compile_service(
        EXAMPLES / "postgres.yaml",
        EXAMPLES / "platform-development.yaml",
        extraction_generation="generation-two",
    )

    with pytest.raises(ConfigurationError):
        validate_deployment_against_artifact(
            deployment,
            replacement,
        )

@pytest.fixture
def lookup(
    compiled: dict[str, Any],
    deployment: dict[str, Any],
):
    def resolve(**overrides):
        request = {
            "environment": deployment["environment"],
            "account_id": deployment["account_id"],
            "region": deployment["region"],
            "source": deployment["source"],
            "database_name": deployment["database_name"],
            "schema_name": "public",
            "table_name": "dms_integration_test",
            "configuration_revision": (
                deployment["configuration_revision"]
            ),
            "artifact_revision": deployment["artifact_revision"],
        }
        request.update(overrides)

        return resolve_table_binding(
            deployment,
            compiled,
            **request,
        )

    return resolve


def test_resolves_table_resources(
    deployment: dict[str, Any],
    lookup,
) -> None:
    result = lookup()
    group = deployment["task_groups"]["integration"]

    assert result["resources"]["replication_task_arn"] == (
        group["replication_task_arn"]
    )
    assert result["landing"] == group["landing"]
    assert result["task_group"]["name"] == "integration"
    assert result["task_group"]["shared"] is False
    assert result["table"]["contract"]["strategy"] == (
        "generate_if_missing"
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("environment", "test"),
        ("account_id", "999900001111"),
        ("region", "eu-west-1"),
        ("source", "another-service"),
        ("database_name", "another_database"),
        ("configuration_revision", "c" * 64),
        ("artifact_revision", "d" * 64),
    ],
)
def test_lookup_rejects_different_pinned_identity(
    lookup,
    field: str,
    replacement: str,
) -> None:
    with pytest.raises(ConfigurationError, match=field):
        lookup(**{field: replacement})


@pytest.mark.parametrize(
    ("schema_name", "table_name"),
    [
        ("public", "unselected_table"),
        ("another_schema", "dms_integration_test"),
        ("PUBLIC", "dms_integration_test"),
    ],
)
def test_lookup_rejects_unselected_table(
    lookup,
    schema_name: str,
    table_name: str,
) -> None:
    with pytest.raises(ConfigurationError, match="not selected"):
        lookup(
            schema_name=schema_name,
            table_name=table_name,
        )


def test_lookup_returns_complete_shared_task_scope(
    compiled: dict[str, Any],
    deployment: dict[str, Any],
    lookup,
) -> None:
    runtime_group = compiled["runtime_configuration"][
        "task_groups"
    ]["integration"]

    other = copy.deepcopy(runtime_group["tables"][0])
    other["table_name"] = "another_table"
    runtime_group["tables"].append(other)

    compiled["module_inputs"]["integration"][
        "replication_tasks"
    ]["integration"]["table_mappings"] = build_table_mappings(
        runtime_group["tables"]
    )

    deployment["task_groups"]["integration"]["tables"].append(
        {
            "schema_name": "public",
            "table_name": "another_table",
        }
    )

    # Recalculate the fixture artifact digest after changing it.
    content = {
        key: value
        for key, value in compiled.items()
        if key != "artifact_revision"
    }
    compiled["artifact_revision"] = fingerprint(content)
    deployment["artifact_revision"] = compiled["artifact_revision"]

    result = lookup()

    assert result["task_group"]["shared"] is True
    assert {
        table["table_name"]
        for table in result["task_group"]["tables"]
    } == {
        "dms_integration_test",
        "another_table",
    }
    assert result["table"]["table_name"] == "dms_integration_test"


def test_lookup_result_is_detached_from_inputs(
    compiled: dict[str, Any],
    deployment: dict[str, Any],
    lookup,
) -> None:
    original_compiled = copy.deepcopy(compiled)
    original_deployment = copy.deepcopy(deployment)

    result = lookup()
    result["landing"]["prefix"] = "changed/"
    result["table"]["contract"]["strategy"] = "changed"
    result["task_group"]["tables"].clear()

    assert compiled == original_compiled
    assert deployment == original_deployment

def refresh_artifact_revision(
    compiled: dict[str, Any],
    deployment: dict[str, Any],
) -> None:
    content = {
        key: value
        for key, value in compiled.items()
        if key != "artifact_revision"
    }
    compiled["artifact_revision"] = fingerprint(content)
    deployment["artifact_revision"] = compiled["artifact_revision"]


def test_runtime_tables_must_match_dms_mappings(
    compiled: dict[str, Any],
    deployment: dict[str, Any],
) -> None:
    compiled["module_inputs"]["integration"][
        "replication_tasks"
    ]["integration"]["table_mappings"] = build_table_mappings(
        [
            {
                "schema_name": "public",
                "table_name": "another_table",
            }
        ]
    )
    refresh_artifact_revision(compiled, deployment)

    with pytest.raises(ConfigurationError, match="table_mappings"):
        validate_deployment_against_artifact(deployment, compiled)


def test_wildcard_mapping_is_rejected_even_with_updated_digest(
    compiled: dict[str, Any],
    deployment: dict[str, Any],
) -> None:
    task = compiled["module_inputs"]["integration"][
        "replication_tasks"
    ]["integration"]

    task["table_mappings"] = (
        '{"rules":[{"rule-type":"selection",'
        '"rule-id":"1","rule-name":"all-tables",'
        '"object-locator":{"schema-name":"public","table-name":"%"},'
        '"rule-action":"include"}]}'
    )
    refresh_artifact_revision(compiled, deployment)

    with pytest.raises(ConfigurationError, match="table_mappings"):
        validate_deployment_against_artifact(deployment, compiled)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("environment", "test"),
        ("name", "another-deployment"),
    ],
)
def test_module_identity_must_match_runtime_configuration(
    compiled: dict[str, Any],
    deployment: dict[str, Any],
    field: str,
    replacement: str,
) -> None:
    compiled["module_inputs"]["integration"][field] = replacement
    refresh_artifact_revision(compiled, deployment)

    with pytest.raises(ConfigurationError, match=field):
        validate_deployment_against_artifact(deployment, compiled)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("engine", "oracle"),
        ("database_name", "another_database"),
    ],
)
def test_module_source_must_match_runtime_configuration(
    compiled: dict[str, Any],
    deployment: dict[str, Any],
    field: str,
    replacement: str,
) -> None:
    compiled["module_inputs"]["integration"][
        "source_endpoint"
    ][field] = replacement
    refresh_artifact_revision(compiled, deployment)

    with pytest.raises(ConfigurationError, match=field):
        validate_deployment_against_artifact(deployment, compiled)


def test_duplicate_runtime_table_is_rejected(
    compiled: dict[str, Any],
    deployment: dict[str, Any],
) -> None:
    tables = compiled["runtime_configuration"][
        "task_groups"
    ]["integration"]["tables"]

    tables.append(copy.deepcopy(tables[0]))
    refresh_artifact_revision(compiled, deployment)

    with pytest.raises(
        ConfigurationError,
        match="duplicate table",
    ):
        validate_deployment_against_artifact(deployment, compiled)

def replacement_deployment(
    deployment: dict[str, Any],
) -> dict[str, Any]:
    replacement = copy.deepcopy(deployment)
    replacement["extraction_generation"] = "generation-two"
    replacement["artifact_revision"] = "e" * 64

    group = replacement["task_groups"]["integration"]

    group["deployment_name"] = "replacement-deployment"
    group["replication_task_id"] = "replacement-task"
    group["replication_task_arn"] = (
        "arn:aws:dms:eu-west-2:111122223333:task:REPLACEMENT"
    )
    group["target_endpoint_arn"] = (
        "arn:aws:dms:eu-west-2:111122223333:endpoint:REPLACEMENT"
    )
    group["landing"]["prefix"] = group["landing"]["prefix"].replace(
        "/generation-one/",
        "/generation-two/",
    )

    return replacement


def check_inventory(
    manifests: list[dict[str, Any]],
) -> None:
    validate_deployment_inventory(
        manifests,
        environment="development",
        account_id="111122223333",
        region="eu-west-2",
    )


def test_separate_generations_are_accepted(
    deployment: dict[str, Any],
) -> None:
    check_inventory(
        [
            deployment,
            replacement_deployment(deployment),
        ]
    )


def test_identical_inventory_record_is_idempotent(
    deployment: dict[str, Any],
) -> None:
    check_inventory(
        [
            deployment,
            copy.deepcopy(deployment),
        ]
    )


def test_generation_record_cannot_be_silently_replaced(
    deployment: dict[str, Any],
) -> None:
    changed = copy.deepcopy(deployment)
    changed["artifact_revision"] = "e" * 64

    with pytest.raises(ConfigurationError, match="Conflicting manifests"):
        check_inventory([deployment, changed])


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("replication_task_arn", "Replication task"),
        ("target_endpoint_arn", "Target endpoint"),
    ],
)
def test_generations_cannot_reuse_extraction_resources(
    deployment: dict[str, Any],
    field: str,
    message: str,
) -> None:
    replacement = replacement_deployment(deployment)
    replacement["task_groups"]["integration"][field] = (
        deployment["task_groups"]["integration"][field]
    )

    with pytest.raises(ConfigurationError, match=message):
        check_inventory([deployment, replacement])


@pytest.mark.parametrize(
    "relationship",
    ["equal", "child", "parent"],
)
def test_generations_cannot_overlap_landing_locations(
    deployment: dict[str, Any],
    relationship: str,
) -> None:
    replacement = replacement_deployment(deployment)
    original_prefix = deployment["task_groups"]["integration"][
        "landing"
    ]["prefix"]

    prefixes = {
        "equal": original_prefix,
        "child": original_prefix + "nested/",
        "parent": "landing/development/",
    }

    replacement["task_groups"]["integration"]["landing"]["prefix"] = (
        prefixes[relationship]
    )

    with pytest.raises(ConfigurationError, match="Landing locations overlap"):
        check_inventory([deployment, replacement])


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("environment", "test"),
        ("account_id", "999900001111"),
        ("region", "eu-west-1"),
    ],
)
def test_inventory_requires_authorised_context(
    deployment: dict[str, Any],
    field: str,
    replacement: str,
) -> None:
    with pytest.raises(ConfigurationError, match=field):
        validate_deployment_inventory(
            [deployment],
            environment=(
                replacement if field == "environment" else "development"
            ),
            account_id=(
                replacement if field == "account_id" else "111122223333"
            ),
            region=(
                replacement if field == "region" else "eu-west-2"
            ),
        )


def test_inventory_validation_preserves_records(
    deployment: dict[str, Any],
) -> None:
    manifests = [
        deployment,
        replacement_deployment(deployment),
    ]
    original = copy.deepcopy(manifests)

    check_inventory(manifests)

    assert manifests == original

@pytest.fixture
def component_catalog(lookup) -> dict[str, Any]:
    binding = lookup()

    return {
        "catalog_version": 1,
        "environment": binding["environment"],
        "account_id": binding["account_id"],
        "region": binding["region"],
        "configuration_revision": binding["configuration_revision"],
        "artifact_revision": binding["artifact_revision"],
        "components": {
            "processing": {
                binding["table"]["processing_profile"]: {
                    "kind": "state_machine",
                    "arn": (
                        "arn:aws:states:eu-west-2:111122223333:"
                        "stateMachine:example-processing"
                    ),
                }
            },
            "raw_history": {
                binding["table"]["history_profile"]: {
                    "kind": "lambda",
                    "arn": (
                        "arn:aws:lambda:eu-west-2:111122223333:"
                        "function:example-history:1"
                    ),
                }
            },
            "publication": {
                binding["table"]["publication_profile"]: {
                    "kind": "lambda",
                    "arn": (
                        "arn:aws:lambda:eu-west-2:111122223333:"
                        "function:example-publication:1"
                    ),
                }
            },
        },
    }


def test_resolves_requested_components(
    lookup,
    component_catalog: dict[str, Any],
) -> None:
    result = resolve_component_bindings(
        lookup(),
        component_catalog,
        required_components=["processing", "raw_history"],
    )

    assert set(result) == {"processing", "raw_history"}
    assert result["processing"]["kind"] == "state_machine"
    assert result["raw_history"]["kind"] == "lambda"


def test_missing_required_component_blocks_lookup(
    lookup,
    component_catalog: dict[str, Any],
) -> None:
    component_catalog["components"]["raw_history"] = {}

    with pytest.raises(
        ConfigurationError,
        match="no deployed binding",
    ):
        resolve_component_bindings(
            lookup(),
            component_catalog,
            required_components=["raw_history"],
        )


@pytest.mark.parametrize(
    "field",
    [
        "environment",
        "account_id",
        "region",
        "configuration_revision",
        "artifact_revision",
    ],
)
def test_component_catalog_must_match_pinned_binding(
    lookup,
    component_catalog: dict[str, Any],
    field: str,
) -> None:
    component_catalog[field] = "different"

    with pytest.raises(ConfigurationError, match=field):
        resolve_component_bindings(
            lookup(),
            component_catalog,
            required_components=["processing"],
        )


@pytest.mark.parametrize(
    "arn",
    [
        "arn:aws:lambda:eu-west-1:111122223333:function:history",
        "arn:aws:lambda:eu-west-2:999900001111:function:history",
        "arn:aws:states:eu-west-2:111122223333:stateMachine:history",
        "not-an-arn",
    ],
)
def test_component_target_must_match_resource_context(
    lookup,
    component_catalog: dict[str, Any],
    arn: str,
) -> None:
    profile = lookup()["table"]["history_profile"]
    component_catalog["components"]["raw_history"][profile][
        "arn"
    ] = arn

    with pytest.raises(ConfigurationError, match="ARN"):
        resolve_component_bindings(
            lookup(),
            component_catalog,
            required_components=["raw_history"],
        )


def test_unknown_component_is_rejected(
    lookup,
    component_catalog: dict[str, Any],
) -> None:
    with pytest.raises(
        ConfigurationError,
        match="Unsupported downstream component",
    ):
        resolve_component_bindings(
            lookup(),
            component_catalog,
            required_components=["unknown"],
        )


def test_component_result_does_not_modify_catalog(
    lookup,
    component_catalog: dict[str, Any],
) -> None:
    original = copy.deepcopy(component_catalog)

    result = resolve_component_bindings(
        lookup(),
        component_catalog,
        required_components=["processing"],
    )
    result["processing"]["arn"] = "changed"

    assert component_catalog == original

@pytest.fixture
def contract_catalog(lookup) -> dict[str, Any]:
    binding = lookup()
    prefix = "arn:aws:lambda:eu-west-2:111122223333:function:"

    return {
        "catalog_version": 1,
        "environment": binding["environment"],
        "account_id": binding["account_id"],
        "region": binding["region"],
        "configuration_revision": binding["configuration_revision"],
        "artifact_revision": binding["artifact_revision"],
        "contracts": {
            "registration": {
                "kind": "lambda",
                "arn": prefix + "example-registration",
            },
            "registry": {
                "table_name": "example-contract-registry",
                "arn": (
                    "arn:aws:dynamodb:eu-west-2:111122223333:"
                    "table/example-contract-registry"
                ),
            },
            "tables": [
                {
                    "source": binding["source"],
                    "database_name": binding["database_name"],
                    "schema_name": binding["table"]["schema_name"],
                    "table_name": binding["table"]["table_name"],
                    "generation": {
                        "kind": "lambda",
                        "arn": prefix + "example-generation",
                    },
                    "validation": {
                        "kind": "lambda",
                        "arn": prefix + "example-validation",
                    },
                }
            ],
        },
    }


def test_resolves_contract_capabilities(
    lookup,
    contract_catalog: dict[str, Any],
) -> None:
    result = resolve_contract_bindings(
        lookup(),
        contract_catalog,
        required_capabilities=[
            "generation",
            "validation",
            "registration",
            "registry",
        ],
    )

    assert set(result) == {
        "generation",
        "validation",
        "registration",
        "registry",
    }


@pytest.mark.parametrize(
    "field",
    ["source", "database_name", "schema_name", "table_name"],
)
def test_contract_table_scope_must_match(
    lookup,
    contract_catalog: dict[str, Any],
    field: str,
) -> None:
    contract_catalog["contracts"]["tables"][0][field] = "different"

    with pytest.raises(ConfigurationError, match="exactly one"):
        resolve_contract_bindings(
            lookup(),
            contract_catalog,
            required_capabilities=["generation"],
        )


def test_duplicate_contract_table_binding_is_rejected(
    lookup,
    contract_catalog: dict[str, Any],
) -> None:
    tables = contract_catalog["contracts"]["tables"]
    tables.append(copy.deepcopy(tables[0]))

    with pytest.raises(ConfigurationError, match="exactly one"):
        resolve_contract_bindings(
            lookup(),
            contract_catalog,
            required_capabilities=["validation"],
        )


def test_missing_generation_binding_blocks_lookup(
    lookup,
    contract_catalog: dict[str, Any],
) -> None:
    del contract_catalog["contracts"]["tables"][0]["generation"]

    with pytest.raises(ConfigurationError, match="no deployed binding"):
        resolve_contract_bindings(
            lookup(),
            contract_catalog,
            required_capabilities=["generation"],
        )


def test_registry_name_and_arn_must_agree(
    lookup,
    contract_catalog: dict[str, Any],
) -> None:
    contract_catalog["contracts"]["registry"]["table_name"] = (
        "another-table"
    )

    with pytest.raises(ConfigurationError, match="registry ARN"):
        resolve_contract_bindings(
            lookup(),
            contract_catalog,
            required_capabilities=["registry"],
        )


def test_contract_lambda_must_match_account(
    lookup,
    contract_catalog: dict[str, Any],
) -> None:
    contract_catalog["contracts"]["registration"]["arn"] = (
        "arn:aws:lambda:eu-west-2:999900001111:"
        "function:another-registration"
    )

    with pytest.raises(ConfigurationError, match="account and region"):
        resolve_contract_bindings(
            lookup(),
            contract_catalog,
            required_capabilities=["registration"],
        )


def test_registry_lookup_does_not_require_generation_binding(
    lookup,
    contract_catalog: dict[str, Any],
) -> None:
    contract_catalog["contracts"]["tables"] = []

    result = resolve_contract_bindings(
        lookup(),
        contract_catalog,
        required_capabilities=["registry"],
    )

    assert set(result) == {"registry"}


def test_contract_lookup_preserves_catalog(
    lookup,
    contract_catalog: dict[str, Any],
) -> None:
    original = copy.deepcopy(contract_catalog)

    result = resolve_contract_bindings(
        lookup(),
        contract_catalog,
        required_capabilities=["registration"],
    )
    result["registration"]["arn"] = "changed"

    assert contract_catalog == original