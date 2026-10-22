from typing import Any

from jsonschema import Draft202012Validator

from ingestion_config.deployment import validate_resource_bindings
from ingestion_config.loader import ConfigurationError
from ingestion_config.schema import load_schema


def validate_deployment_inventory(
    manifests: list[dict[str, Any]],
    *,
    environment: str,
    account_id: str,
    region: str,
) -> None:
    validator = Draft202012Validator(
        load_schema("deployment-v1.schema.json")
    )

    deployments: dict[tuple[str, str, str], dict[str, Any]] = {}
    boundaries: list[dict[str, Any]] = []

    for manifest in manifests:
        error = next(validator.iter_errors(manifest), None)

        if error is not None:
            raise ConfigurationError(
                f"Invalid deployment inventory manifest: {error.message}"
            )

        validate_resource_bindings(manifest)

        for field, expected in (
            ("environment", environment),
            ("account_id", account_id),
            ("region", region),
        ):
            if manifest[field] != expected:
                raise ConfigurationError(
                    f"Deployment inventory {field} does not match "
                    "the authorised deployment context"
                )

        identity = (
            manifest["source"],
            manifest["database_name"],
            manifest["extraction_generation"],
        )

        existing = deployments.get(identity)

        if existing is not None:
            if existing != manifest:
                raise ConfigurationError(
                    f"Conflicting manifests for deployment "
                    f"{identity!r}; an admitted extraction generation "
                    "must retain its original deployment record"
                )

            # Repeated delivery of the same record is harmless
            continue

        deployments[identity] = manifest

        for group_name, group in manifest["task_groups"].items():
            current = {
                "identity": identity,
                "group": group_name,
                "task": group["replication_task_arn"],
                "target": group["target_endpoint_arn"],
                "bucket": group["landing"]["bucket_name"],
                "prefix": group["landing"]["prefix"],
            }

            for previous in boundaries:
                if current["task"] == previous["task"]:
                    raise ConfigurationError(
                        "Replication task is reused across deployment "
                        f"boundaries: {previous['identity']!r} and "
                        f"{identity!r}"
                    )

                if current["target"] == previous["target"]:
                    raise ConfigurationError(
                        "Target endpoint is reused across deployment "
                        f"boundaries: {previous['identity']!r} and "
                        f"{identity!r}"
                    )

                if (
                    current["bucket"] == previous["bucket"]
                    and (
                        current["prefix"].startswith(previous["prefix"])
                        or previous["prefix"].startswith(current["prefix"])
                    )
                ):
                    raise ConfigurationError(
                        "Landing locations overlap across deployment "
                        f"boundaries: {previous['identity']!r} and "
                        f"{identity!r}"
                    )

            boundaries.append(current)