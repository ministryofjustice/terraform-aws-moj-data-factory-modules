import copy
import json
from typing import Any

from jsonschema import Draft202012Validator

from ingestion_config.bindings import (
    require_equal,
    validate_deployment_against_artifact,
)
from ingestion_config.deployment import validate_resource_bindings
from ingestion_config.loader import ConfigurationError
from ingestion_config.schema import load_schema


def build_deployment_manifest(
    artifact: dict[str, Any],
    deployment_bindings: dict[str, Any],
    *,
    account_id: str,
) -> dict[str, Any]:
    try:
        return _build_manifest(
            artifact,
            deployment_bindings,
            account_id=account_id,
        )
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        if isinstance(error, ConfigurationError):
            raise

        raise ConfigurationError(
            "Cannot build deployment manifest: "
            "incomplete or invalid compiler artifact "
            "or Terraform deployment bindings"
        ) from error


def _build_manifest(
    artifact: dict[str, Any],
    deployment_bindings: dict[str, Any],
    *,
    account_id: str,
) -> dict[str, Any]:
    runtime = artifact["runtime_configuration"]
    module_inputs = artifact["module_inputs"]

    require_equal(
        set(deployment_bindings),
        set(runtime["task_groups"]),
        "deployment_bindings.task_groups",
    )
    require_equal(
        set(module_inputs),
        set(runtime["task_groups"]),
        "artifact.module_inputs",
    )

    groups = {}

    for group_name, expected in runtime["task_groups"].items():
        deployed = deployment_bindings[group_name]
        inputs = module_inputs[group_name]
        location = f"deployment_bindings.{group_name}"

        require_equal(
            deployed["deployment_name"],
            inputs["name"],
            f"{location}.deployment_name",
        )
        require_equal(
            deployed["environment"],
            runtime["environment"],
            f"{location}.environment",
        )
        require_equal(
            inputs["environment"],
            runtime["environment"],
            f"artifact.module_inputs.{group_name}.environment",
        )

        source = deployed["source_endpoint"]
        target = deployed["target_endpoint"]

        for field in ("engine", "database_name"):
            require_equal(
                source[field],
                runtime["source_endpoint"][field],
                f"{location}.source_endpoint.{field}",
            )
            require_equal(
                source[field],
                inputs["source_endpoint"][field],
                f"{location}.source_endpoint.{field}",
            )

        for field in ("bucket_name", "bucket_folder"):
            require_equal(
                target[field],
                inputs["target"][field],
                f"{location}.target_endpoint.{field}",
            )

        if target["cdc_path"] not in (None, ""):
            raise ConfigurationError(
                f"{location}.target_endpoint.cdc_path: "
                "separate CDC paths are not supported "
                "by the current compiled configuration"
            )

        require_equal(
            set(deployed["replication_tasks"]),
            {group_name},
            f"{location}.replication_tasks",
        )
        require_equal(
            set(inputs["replication_tasks"]),
            {group_name},
            f"artifact.module_inputs.{group_name}.replication_tasks",
        )

        task = deployed["replication_tasks"][group_name]
        expected_task = inputs["replication_tasks"][group_name]

        require_equal(
            task["id"],
            expected_task["replication_task_id"],
            f"{location}.replication_task_id",
        )
        require_equal(
            task["migration_type"],
            expected_task["migration_type"],
            f"{location}.migration_type",
        )

        require_equal(
            task["replication_instance_arn"],
            deployed["replication_instance_arn"],
            f"{location}.task.replication_instance_arn",
        )
        require_equal(
            task["source_endpoint_arn"],
            source["arn"],
            f"{location}.task.source_endpoint_arn",
        )
        require_equal(
            task["target_endpoint_arn"],
            target["arn"],
            f"{location}.task.target_endpoint_arn",
        )

        require_equal(
            task["table_mappings"],
            json.loads(expected_task["table_mappings"]),
            f"{location}.table_mappings",
        )

        groups[group_name] = {
            "deployment_name": deployed["deployment_name"],
            "migration_type": task["migration_type"],
            "replication_instance_arn": (
                deployed["replication_instance_arn"]
            ),
            "source_endpoint_arn": source["arn"],
            "target_endpoint_arn": target["arn"],
            "replication_task_arn": task["arn"],
            "replication_task_id": task["id"],
            "tables": [
                {
                    "schema_name": table["schema_name"],
                    "table_name": table["table_name"],
                }
                for table in expected["tables"]
            ],
            "landing": {
                "bucket_name": target["bucket_name"],
                "prefix": target["bucket_folder"] + "/",
            },
        }

    manifest = {
        "manifest_version": 1,
        "configuration_revision": artifact["configuration_revision"],
        "artifact_revision": artifact["artifact_revision"],
        "environment": runtime["environment"],
        "account_id": account_id,
        "region": runtime["region"],
        "source": runtime["source"],
        "database_name": (
            runtime["source_endpoint"]["database_name"]
        ),
        "extraction_generation": artifact["extraction_generation"],
        "task_groups": groups,
    }

    validator = Draft202012Validator(
        load_schema("deployment-v1.schema.json")
    )

    errors = sorted(
        validator.iter_errors(manifest),
        key=lambda error: tuple(
            str(part) for part in error.absolute_path
        ),
    )

    if errors:
        error = errors[0]
        location = ".".join(
            str(part) for part in error.absolute_path
        ) or "<root>"

        raise ConfigurationError(
            f"deployment manifest {location}: {error.message}"
        )

    validate_resource_bindings(manifest)
    validate_deployment_against_artifact(manifest, artifact)

    return copy.deepcopy(manifest)