import copy
from typing import Any

from jsonschema import Draft202012Validator

from ingestion_config.bindings import (
    validate_deployment_against_artifact,
)
from ingestion_config.deployment import validate_resource_bindings
from ingestion_config.loader import ConfigurationError
from ingestion_config.schema import load_schema


def resolve_table_binding(
    manifest: dict[str, Any],
    artifact: dict[str, Any],
    *,
    environment: str,
    account_id: str,
    region: str,
    source: str,
    database_name: str,
    schema_name: str,
    table_name: str,
    configuration_revision: str,
    artifact_revision: str,
) -> dict[str, Any]:
    validator = Draft202012Validator(
        load_schema("deployment-v1.schema.json")
    )
    error = next(validator.iter_errors(manifest), None)

    if error is not None:
        location = ".".join(
            str(part) for part in error.absolute_path
        ) or "<root>"

        raise ConfigurationError(
            f"deployment manifest {location}: {error.message}"
        )

    validate_resource_bindings(manifest)
    validate_deployment_against_artifact(manifest, artifact)

    expected_identity = {
        "environment": environment,
        "account_id": account_id,
        "region": region,
        "source": source,
        "database_name": database_name,
        "configuration_revision": configuration_revision,
        "artifact_revision": artifact_revision,
    }

    for field, expected in expected_identity.items():
        if manifest[field] != expected:
            raise ConfigurationError(
                f"request.{field}: does not match "
                "the pinned deployment manifest"
            )

    matches = []

    for group_name, group in manifest["task_groups"].items():
        for table in group["tables"]:
            if (
                table["schema_name"] == schema_name
                and table["table_name"] == table_name
            ):
                matches.append((group_name, group))

    if not matches:
        raise ConfigurationError(
            f"Table {schema_name}.{table_name} is not selected "
            f"for source {source!r} in this deployment"
        )

    if len(matches) != 1:
        raise ConfigurationError(
            f"Table {schema_name}.{table_name} is assigned "
            "to more than one task group"
        )

    group_name, group = matches[0]

    configured_tables = artifact["runtime_configuration"][
        "task_groups"
    ][group_name]["tables"]

    selected = [
        table
        for table in configured_tables
        if (
            table["schema_name"] == schema_name
            and table["table_name"] == table_name
        )
    ]

    if len(selected) != 1:
        raise ConfigurationError(
            f"Table {schema_name}.{table_name} must have "
            "exactly one compiled configuration"
        )

    result = {
        "environment": manifest["environment"],
        "account_id": manifest["account_id"],
        "region": manifest["region"],
        "source": manifest["source"],
        "database_name": manifest["database_name"],
        "configuration_revision": manifest[
            "configuration_revision"
        ],
        "artifact_revision": manifest["artifact_revision"],
        "extraction_generation": manifest[
            "extraction_generation"
        ],
        "table": selected[0],
        "task_group": {
            "name": group_name,
            "migration_type": group["migration_type"],
            "tables": group["tables"],
            "shared": len(group["tables"]) > 1,
        },
        "resources": {
            "deployment_name": group["deployment_name"],
            "replication_instance_arn": group[
                "replication_instance_arn"
            ],
            "source_endpoint_arn": group["source_endpoint_arn"],
            "target_endpoint_arn": group["target_endpoint_arn"],
            "replication_task_arn": group["replication_task_arn"],
            "replication_task_id": group["replication_task_id"],
        },
        "landing": group["landing"],
    }

    return copy.deepcopy(result)