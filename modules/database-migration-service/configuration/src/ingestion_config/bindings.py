import json
from typing import Any

from ingestion_config.compiler import fingerprint
from ingestion_config.loader import ConfigurationError
from ingestion_config.mappings import build_table_mappings


def require_equal(
    actual: Any,
    expected: Any,
    location: str,
) -> None:
    if actual != expected:
        raise ConfigurationError(
            f"{location}: does not match the compiled artifact"
        )


def validate_deployment_against_artifact(
    manifest: dict[str, Any],
    artifact: dict[str, Any],
) -> None:
    try:
        _validate_deployment_against_artifact(manifest, artifact)
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        if isinstance(error, ConfigurationError):
            raise

        raise ConfigurationError(
            "Deployment comparison requires a complete manifest "
            "and a valid compiled artifact"
        ) from error


def _validate_deployment_against_artifact(
    manifest: dict[str, Any],
    artifact: dict[str, Any],
) -> None:
    content = {
        key: value
        for key, value in artifact.items()
        if key != "artifact_revision"
    }

    require_equal(
        artifact["artifact_revision"],
        fingerprint(content),
        "artifact.artifact_revision",
    )
    require_equal(
        artifact["artifact_version"],
        1,
        "artifact.artifact_version",
    )

    runtime = artifact["runtime_configuration"]
    inputs = artifact["module_inputs"]

    for field in (
        "configuration_revision",
        "extraction_generation",
    ):
        require_equal(
            runtime[field],
            artifact[field],
            f"artifact.runtime_configuration.{field}",
        )
        require_equal(
            manifest[field],
            artifact[field],
            f"manifest.{field}",
        )

    require_equal(
        manifest["artifact_revision"],
        artifact["artifact_revision"],
        "manifest.artifact_revision",
    )

    for field in ("environment", "region", "source"):
        require_equal(
            manifest[field],
            runtime[field],
            f"manifest.{field}",
        )

    require_equal(
        manifest["database_name"],
        runtime["source_endpoint"]["database_name"],
        "manifest.database_name",
    )
    require_equal(
        set(manifest["task_groups"]),
        set(runtime["task_groups"]),
        "manifest.task_groups",
    )
    require_equal(
        set(inputs),
        set(runtime["task_groups"]),
        "artifact.module_inputs",
    )

    seen_tables: dict[tuple[str, str], str] = {}

    for name, expected in runtime["task_groups"].items():
        actual = manifest["task_groups"][name]
        module = inputs[name]

        location = f"manifest.task_groups.{name}"
        module_location = f"artifact.module_inputs.{name}"

        require_equal(
            set(module["replication_tasks"]),
            {name},
            f"{module_location}.replication_tasks",
        )

        task = module["replication_tasks"][name]

        expected_tables = [
            (table["schema_name"], table["table_name"])
            for table in expected["tables"]
        ]
        actual_tables = [
            (table["schema_name"], table["table_name"])
            for table in actual["tables"]
        ]

        if len(expected_tables) != len(set(expected_tables)):
            raise ConfigurationError(
                f"artifact.runtime_configuration.task_groups."
                f"{name}.tables: duplicate table selection"
            )

        if len(actual_tables) != len(set(actual_tables)):
            raise ConfigurationError(
                f"{location}.tables: duplicate table selection"
            )

        for identity in expected_tables:
            if identity in seen_tables:
                raise ConfigurationError(
                    f"Table {identity[0]}.{identity[1]} "
                    f"is assigned to task groups "
                    f"{seen_tables[identity]!r} and {name!r}"
                )

            seen_tables[identity] = name

        require_equal(
            set(actual_tables),
            set(expected_tables),
            f"{location}.tables",
        )

        require_equal(
            json.loads(task["table_mappings"]),
            json.loads(build_table_mappings(expected["tables"])),
            f"{module_location}.table_mappings",
        )

        for field, expected_value in (
            ("deployment_name", expected["deployment_name"]),
            ("migration_type", expected["mode"]),
            ("replication_task_id", expected["replication_task_id"]),
            ("landing", expected["landing"]),
        ):
            require_equal(
                actual[field],
                expected_value,
                f"{location}.{field}",
            )

        require_equal(
            module["name"],
            expected["deployment_name"],
            f"{module_location}.name",
        )
        require_equal(
            module["environment"],
            runtime["environment"],
            f"{module_location}.environment",
        )

        for field in ("engine", "database_name"):
            require_equal(
                module["source_endpoint"][field],
                runtime["source_endpoint"][field],
                f"{module_location}.source_endpoint.{field}",
            )

        require_equal(
            task["replication_task_id"],
            expected["replication_task_id"],
            f"{module_location}.replication_task_id",
        )
        require_equal(
            task["migration_type"],
            expected["mode"],
            f"{module_location}.migration_type",
        )
        require_equal(
            module["target"]["bucket_name"],
            expected["landing"]["bucket_name"],
            f"{module_location}.target.bucket_name",
        )
        require_equal(
            module["target"]["bucket_folder"] + "/",
            expected["landing"]["prefix"],
            f"{module_location}.target.bucket_folder",
        )