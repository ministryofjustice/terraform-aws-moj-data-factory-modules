import copy
import json
from pathlib import Path
from typing import Any

import pytest

from ingestion_config.compiler import compile_service
from ingestion_config.loader import ConfigurationError
from ingestion_config.manifest_builder import (
    build_deployment_manifest,
)


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
ACCOUNT_ID = "111122223333"


@pytest.fixture(params=["postgres", "oracle"])
def deployment_inputs(
    request: pytest.FixtureRequest,
) -> tuple[dict[str, Any], dict[str, Any]]:
    artifact = compile_service(
        EXAMPLES / f"{request.param}.yaml",
        EXAMPLES / "platform-development.yaml",
        extraction_generation="generation-one",
    )

    bindings = {}

    for group_name, inputs in artifact["module_inputs"].items():
        arn_prefix = (
            f"arn:aws:dms:eu-west-2:{ACCOUNT_ID}"
        )

        instance_arn = f"{arn_prefix}:rep:EXAMPLEINSTANCE"
        source_arn = f"{arn_prefix}:endpoint:EXAMPLESOURCE"
        target_arn = f"{arn_prefix}:endpoint:EXAMPLETARGET"

        task = inputs["replication_tasks"][group_name]

        bindings[group_name] = {
            "deployment_name": inputs["name"],
            "environment": inputs["environment"],
            "replication_instance_arn": instance_arn,
            "source_endpoint": {
                "arn": source_arn,
                "engine": inputs["source_endpoint"]["engine"],
                "database_name": (
                    inputs["source_endpoint"]["database_name"]
                ),
            },
            "target_endpoint": {
                "arn": target_arn,
                "bucket_name": inputs["target"]["bucket_name"],
                "bucket_folder": inputs["target"]["bucket_folder"],
                "cdc_path": None,
            },
            "replication_tasks": {
                group_name: {
                    "arn": f"{arn_prefix}:task:EXAMPLETASK",
                    "id": task["replication_task_id"],
                    "migration_type": task["migration_type"],
                    "replication_instance_arn": instance_arn,
                    "source_endpoint_arn": source_arn,
                    "target_endpoint_arn": target_arn,
                    "table_mappings": json.loads(
                        task["table_mappings"]
                    ),
                }
            },
        }

    return artifact, bindings


def test_builds_manifest_for_configured_engine(
    deployment_inputs,
) -> None:
    artifact, bindings = deployment_inputs

    manifest = build_deployment_manifest(
        artifact,
        bindings,
        account_id=ACCOUNT_ID,
    )

    group = manifest["task_groups"]["integration"]
    runtime = artifact["runtime_configuration"]

    assert manifest["source"] == runtime["source"]
    assert manifest["configuration_revision"] == (
        artifact["configuration_revision"]
    )
    assert manifest["artifact_revision"] == (
        artifact["artifact_revision"]
    )
    assert group["landing"] == (
        runtime["task_groups"]["integration"]["landing"]
    )
    assert group["replication_task_arn"] == (
        bindings["integration"]["replication_tasks"][
            "integration"
        ]["arn"]
    )


@pytest.mark.parametrize(
    "field",
    [
        "replication_instance_arn",
        "source_endpoint_arn",
        "target_endpoint_arn",
    ],
)
def test_task_must_use_exported_resources(
    deployment_inputs,
    field: str,
) -> None:
    artifact, bindings = deployment_inputs
    task = bindings["integration"]["replication_tasks"][
        "integration"
    ]
    task[field] += "OTHER"

    with pytest.raises(ConfigurationError, match=field):
        build_deployment_manifest(
            artifact,
            bindings,
            account_id=ACCOUNT_ID,
        )


@pytest.mark.parametrize(
    "field",
    ["bucket_name", "bucket_folder"],
)
def test_different_target_location_is_rejected(
    deployment_inputs,
    field: str,
) -> None:
    artifact, bindings = deployment_inputs
    bindings["integration"]["target_endpoint"][field] = (
        "another-location"
    )

    with pytest.raises(ConfigurationError, match=field):
        build_deployment_manifest(
            artifact,
            bindings,
            account_id=ACCOUNT_ID,
        )


def test_broader_table_mapping_is_rejected(
    deployment_inputs,
) -> None:
    artifact, bindings = deployment_inputs
    mappings = bindings["integration"]["replication_tasks"][
        "integration"
    ]["table_mappings"]

    mappings["rules"][0]["rule-action"] = "include"
    mappings["rules"][0]["object-locator"]["table-name"] = "%"

    with pytest.raises(ConfigurationError, match="table_mappings"):
        build_deployment_manifest(
            artifact,
            bindings,
            account_id=ACCOUNT_ID,
        )


def test_different_source_database_is_rejected(
    deployment_inputs,
) -> None:
    artifact, bindings = deployment_inputs
    bindings["integration"]["source_endpoint"]["database_name"] = (
        "another_database"
    )

    with pytest.raises(ConfigurationError, match="database_name"):
        build_deployment_manifest(
            artifact,
            bindings,
            account_id=ACCOUNT_ID,
        )


def test_unmodelled_cdc_location_is_rejected(
    deployment_inputs,
) -> None:
    artifact, bindings = deployment_inputs
    bindings["integration"]["target_endpoint"]["cdc_path"] = (
        "another-cdc-location"
    )

    with pytest.raises(ConfigurationError, match="cdc_path"):
        build_deployment_manifest(
            artifact,
            bindings,
            account_id=ACCOUNT_ID,
        )


def test_account_mismatch_is_rejected(
    deployment_inputs,
) -> None:
    artifact, bindings = deployment_inputs

    with pytest.raises(ConfigurationError, match="account"):
        build_deployment_manifest(
            artifact,
            bindings,
            account_id="999900001111",
        )


def test_missing_deployment_group_is_rejected(
    deployment_inputs,
) -> None:
    artifact, _ = deployment_inputs

    with pytest.raises(ConfigurationError, match="task_groups"):
        build_deployment_manifest(
            artifact,
            {},
            account_id=ACCOUNT_ID,
        )


def test_additional_deployment_group_is_rejected(
    deployment_inputs,
) -> None:
    artifact, bindings = deployment_inputs
    bindings["unexpected"] = copy.deepcopy(
        bindings["integration"]
    )

    with pytest.raises(ConfigurationError, match="task_groups"):
        build_deployment_manifest(
            artifact,
            bindings,
            account_id=ACCOUNT_ID,
        )


def test_builder_does_not_modify_inputs(
    deployment_inputs,
) -> None:
    artifact, bindings = deployment_inputs
    original_artifact = copy.deepcopy(artifact)
    original_bindings = copy.deepcopy(bindings)

    build_deployment_manifest(
        artifact,
        bindings,
        account_id=ACCOUNT_ID,
    )

    assert artifact == original_artifact
    assert bindings == original_bindings


def test_incomplete_binding_has_configuration_error(
    deployment_inputs,
) -> None:
    artifact, bindings = deployment_inputs
    del bindings["integration"]["target_endpoint"]

    with pytest.raises(
        ConfigurationError,
        match="incomplete or invalid",
    ):
        build_deployment_manifest(
            artifact,
            bindings,
            account_id=ACCOUNT_ID,
        )