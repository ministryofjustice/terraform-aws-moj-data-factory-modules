import json
from pathlib import Path
from typing import Any

import pytest
import copy

from ingestion_config.deployment import load_deployment
from ingestion_config.loader import ConfigurationError
from ingestion_config.schema import load_schema


@pytest.fixture
def manifest() -> dict[str, Any]:
    resource_prefix = (
        "arn:aws:dms:eu-west-2:111122223333"
    )

    return {
        "manifest_version": 1,
        "configuration_revision": "a" * 64,
        "artifact_revision": "b" * 64,
        "environment": "development",
        "account_id": "111122223333",
        "region": "eu-west-2",
        "source": "sandbox-postgres",
        "database_name": "dms_test",
        "extraction_generation": "generation-one",
        "task_groups": {
            "integration": {
                "deployment_name": "example-deployment",
                "migration_type": "full-load-and-cdc",
                "replication_instance_arn": (
                    f"{resource_prefix}:rep:EXAMPLEINSTANCE"
                ),
                "source_endpoint_arn": (
                    f"{resource_prefix}:endpoint:EXAMPLESOURCE"
                ),
                "target_endpoint_arn": (
                    f"{resource_prefix}:endpoint:EXAMPLETARGET"
                ),
                "replication_task_arn": (
                    f"{resource_prefix}:task:EXAMPLETASK"
                ),
                "replication_task_id": "example-task",
                "tables": [
                    {
                        "schema_name": "public",
                        "table_name": "dms_integration_test",
                    }
                ],
                "landing": {
                    "bucket_name": "example-development-raw",
                    "prefix": (
                        "landing/development/sandbox-postgres/"
                        "dms_test/integration/generation-one/"
                    ),
                },
            }
        },
    }


def write_manifest(
    tmp_path: Path,
    manifest: dict[str, Any],
) -> Path:
    path = tmp_path / "deployment.json"
    path.write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    return path


def test_deployment_schema_is_bundled() -> None:
    schema = load_schema("deployment-v1.schema.json")

    assert schema["title"] == "Ingestion deployment manifest"


def test_load_deployment(
    tmp_path: Path,
    manifest: dict[str, Any],
) -> None:
    path = write_manifest(tmp_path, manifest)

    assert load_deployment(path) == manifest


@pytest.mark.parametrize(
    "field",
    [
        "configuration_revision",
        "artifact_revision",
        "account_id",
        "extraction_generation",
        "task_groups",
    ],
)
def test_missing_required_field(
    tmp_path: Path,
    manifest: dict[str, Any],
    field: str,
) -> None:
    del manifest[field]
    path = write_manifest(tmp_path, manifest)

    with pytest.raises(ConfigurationError, match=field):
        load_deployment(path)


def test_unknown_field_is_rejected(
    tmp_path: Path,
    manifest: dict[str, Any],
) -> None:
    manifest["ingestion_version"] = 1
    path = write_manifest(tmp_path, manifest)

    with pytest.raises(
        ConfigurationError,
        match="ingestion_version",
    ):
        load_deployment(path)


def test_duplicate_json_keys_are_rejected(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment.json"
    path.write_text(
        '{"manifest_version": 1, "manifest_version": 1}',
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigurationError,
        match="Duplicate key",
    ):
        load_deployment(path)

@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        (
            "replication_instance_arn",
            "arn:aws:dms:eu-west-2:111122223333:task:WRONGTYPE",
        ),
        (
            "source_endpoint_arn",
            "arn:aws:dms:eu-west-2:111122223333:rep:WRONGTYPE",
        ),
        (
            "target_endpoint_arn",
            "arn:aws:dms:eu-west-2:999900001111:endpoint:OTHERACCOUNT",
        ),
        (
            "replication_task_arn",
            "arn:aws:dms:eu-west-1:111122223333:task:OTHERREGION",
        ),
    ],
)
def test_inconsistent_resource_arn_is_rejected(
    tmp_path: Path,
    manifest: dict[str, Any],
    field: str,
    replacement: str,
) -> None:
    manifest["task_groups"]["integration"][field] = replacement
    path = write_manifest(tmp_path, manifest)

    with pytest.raises(ConfigurationError, match=field):
        load_deployment(path)


@pytest.mark.parametrize(
    "prefix",
    [
        "landing//generation-one/",
        "landing/./generation-one/",
        "landing/../generation-one/",
        "landing\\generation-one/",
        "landing/ generation-one/",
    ],
)
def test_ambiguous_landing_prefix_is_rejected(
    tmp_path: Path,
    manifest: dict[str, Any],
    prefix: str,
) -> None:
    manifest["task_groups"]["integration"]["landing"]["prefix"] = (
        prefix
    )
    path = write_manifest(tmp_path, manifest)

    with pytest.raises(
        ConfigurationError,
        match="landing.prefix",
    ):
        load_deployment(path)


def add_second_group(
    manifest: dict[str, Any],
) -> dict[str, Any]:
    group = copy.deepcopy(
        manifest["task_groups"]["integration"]
    )

    group["deployment_name"] = "example-second-deployment"
    group["replication_task_id"] = "example-second-task"
    group["replication_task_arn"] = (
        "arn:aws:dms:eu-west-2:111122223333:task:SECONDTASK"
    )
    group["target_endpoint_arn"] = (
        "arn:aws:dms:eu-west-2:111122223333:endpoint:SECONDTARGET"
    )
    group["tables"] = [
        {
            "schema_name": "public",
            "table_name": "another_table",
        }
    ]
    group["landing"]["prefix"] = (
        "landing/development/sandbox-postgres/"
        "dms_test/second/generation-one/"
    )

    manifest["task_groups"]["second"] = group
    return group


@pytest.mark.parametrize(
    "field",
    [
        "replication_task_arn",
        "target_endpoint_arn",
    ],
)
def test_exclusive_resource_cannot_be_shared(
    tmp_path: Path,
    manifest: dict[str, Any],
    field: str,
) -> None:
    second = add_second_group(manifest)
    second[field] = manifest["task_groups"]["integration"][field]
    path = write_manifest(tmp_path, manifest)

    with pytest.raises(
        ConfigurationError,
        match="already assigned",
    ):
        load_deployment(path)


@pytest.mark.parametrize(
    "relationship",
    ["equal", "child", "parent"],
)
def test_overlapping_landings_are_rejected(
    tmp_path: Path,
    manifest: dict[str, Any],
    relationship: str,
) -> None:
    second = add_second_group(manifest)
    first_prefix = (
        manifest["task_groups"]["integration"]["landing"]["prefix"]
    )

    prefixes = {
        "equal": first_prefix,
        "child": first_prefix + "nested/",
        "parent": "landing/development/",
    }

    second["landing"]["prefix"] = prefixes[relationship]
    path = write_manifest(tmp_path, manifest)

    with pytest.raises(ConfigurationError, match="overlaps"):
        load_deployment(path)


def test_instance_and_source_endpoint_can_be_shared(
    tmp_path: Path,
    manifest: dict[str, Any],
) -> None:
    add_second_group(manifest)
    path = write_manifest(tmp_path, manifest)

    assert load_deployment(path) == manifest


def test_same_prefix_in_different_buckets_is_separate(
    tmp_path: Path,
    manifest: dict[str, Any],
) -> None:
    second = add_second_group(manifest)
    second["landing"] = {
        "bucket_name": "example-second-raw",
        "prefix": (
            manifest["task_groups"]["integration"]["landing"]["prefix"]
        ),
    }
    path = write_manifest(tmp_path, manifest)

    assert load_deployment(path) == manifest