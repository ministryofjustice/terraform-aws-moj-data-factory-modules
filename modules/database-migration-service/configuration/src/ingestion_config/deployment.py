from pathlib import Path
from typing import Any

from ingestion_config.loader import (
    ConfigurationError,
    load_document,
)


DMS_RESOURCE_TYPES = {
    "replication_instance_arn": "rep",
    "source_endpoint_arn": "endpoint",
    "target_endpoint_arn": "endpoint",
    "replication_task_arn": "task",
}


def validate_resource_bindings(
    manifest: dict[str, Any],
) -> None:
    """Check internal consistency after JSON Schema validation."""
    region = manifest["region"]
    account_id = manifest["account_id"]

    tasks: dict[str, str] = {}
    targets: dict[str, str] = {}
    landings: list[tuple[str, str, str]] = []

    for group_name, group in manifest["task_groups"].items():
        for field, resource_type in DMS_RESOURCE_TYPES.items():
            expected_prefix = (
                f"arn:aws:dms:{region}:{account_id}:"
                f"{resource_type}:"
            )

            if not group[field].startswith(expected_prefix):
                raise ConfigurationError(
                    f"task_groups.{group_name}.{field}: "
                    "must identify the expected DMS resource type "
                    "in the manifest account and region"
                )

        for field, seen in (
            ("replication_task_arn", tasks),
            ("target_endpoint_arn", targets),
        ):
            resource_arn = group[field]

            if resource_arn in seen:
                raise ConfigurationError(
                    f"task_groups.{group_name}.{field}: "
                    f"already assigned to task group "
                    f"{seen[resource_arn]!r}"
                )

            seen[resource_arn] = group_name

        landing = group["landing"]
        bucket_name = landing["bucket_name"]
        prefix = landing["prefix"]

        segments = prefix[:-1].split("/")

        if (
            not prefix.endswith("/")
            or "\\" in prefix
            or any(
                segment in ("", ".", "..")
                or segment != segment.strip()
                for segment in segments
            )
        ):
            raise ConfigurationError(
                f"task_groups.{group_name}.landing.prefix: "
                "must contain explicit path segments, "
                "end with '/', and contain no empty segments, "
                "relative segments, backslashes or surrounding "
                "segment whitespace"
            )

        for other_bucket, other_prefix, other_group in landings:
            if (
                bucket_name == other_bucket
                and (
                    prefix.startswith(other_prefix)
                    or other_prefix.startswith(prefix)
                )
            ):
                raise ConfigurationError(
                    f"task_groups.{group_name}.landing.prefix: "
                    f"overlaps the landing location for "
                    f"task group {other_group!r}"
                )

        landings.append(
            (bucket_name, prefix, group_name)
        )


def load_deployment(
    path: str | Path,
) -> dict[str, Any]:
    manifest = load_document(
        path,
        "deployment-v1.schema.json",
    )

    try:
        validate_resource_bindings(manifest)
    except ConfigurationError as error:
        raise ConfigurationError(
            f"{Path(path)}: {error}"
        ) from error

    return manifest