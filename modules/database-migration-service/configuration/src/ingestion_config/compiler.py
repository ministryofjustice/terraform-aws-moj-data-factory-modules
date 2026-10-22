import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from ingestion_config.loader import ConfigurationError
from ingestion_config.mappings import build_table_mappings
from ingestion_config.naming import (
    NAMING_VERSION,
    build_resource_names,
)
from ingestion_config.platform import resolve_service


COMPILER_VERSION = "0.1.0"
ARTIFACT_VERSION = 1


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        canonical_json(value).encode("utf-8")
    ).hexdigest()


def compile_service(
    service_path: str | Path,
    platform_path: str | Path,
    *,
    extraction_generation: str,
) -> dict[str, Any]:
    resolved = copy.deepcopy(
        resolve_service(service_path, platform_path)
    )

    service = resolved["service_configuration"]
    bindings = resolved["bindings"]

    for group in service["task_groups"].values():
        group["tables"].sort(
            key=lambda table: (
                table["schema_name"],
                table["table_name"],
            )
        )

    for field in (
        "subnet_ids",
        "additional_security_group_ids",
    ):
        bindings["network"][field] = sorted(
            bindings["network"][field]
        )

    service["configuration_version"] = int(
        service["configuration_version"]
    )

    bindings["replication_instance"]["allocated_storage"] = int(
        bindings["replication_instance"]["allocated_storage"]
    )

    for field in (
        "max_parallel_tables",
        "max_parallel_full_loads",
    ):
        bindings["concurrency"][field] = int(
            bindings["concurrency"][field]
        )

    configuration_revision = fingerprint(
        {
            "artifact_version": ARTIFACT_VERSION,
            "compiler_version": COMPILER_VERSION,
            "naming_version": NAMING_VERSION,
            "resolved_configuration": resolved,
        }
    )

    module_inputs = {}
    groups = {}
    seen_names: set[str] = set()

    for group_name, group in sorted(
        service["task_groups"].items()
    ):
        names = build_resource_names(
            environment=bindings["environment"],
            region=bindings["region"],
            service=service["service"],
            database_name=service["source"]["database_name"],
            task_group=group_name,
            extraction_generation=extraction_generation,
        )

        if names["name"] in seen_names:
            raise ConfigurationError(
                "Generated deployment name collision"
            )

        seen_names.add(names["name"])

        destination = bindings["destination"]

        bucket_folder = (
            destination["landing_prefix"]
            + "/".join(
                [
                    bindings["environment"],
                    service["service"],
                    service["source"]["database_name"],
                    group_name,
                    extraction_generation,
                ]
            )
        )

        module_inputs[group_name] = {
            "name": names["name"],
            "environment": bindings["environment"],
            "vpc_id": bindings["vpc_id"],
            "network": copy.deepcopy(
                bindings["network"]
            ),
            "replication_instance": copy.deepcopy(
                bindings["replication_instance"]
            ),
            "source_endpoint": copy.deepcopy(
                bindings["source_endpoint"]
            ),
            "target": {
                "bucket_name": destination["bucket_name"],
                "bucket_folder": bucket_folder,
                "encryption": {
                    "mode": "SSE_KMS",
                    "kms_key_arn": destination["kms_key_arn"],
                },
            },
            "replication_tasks": {
                group_name: {
                    "replication_task_id": (
                        names["replication_task_id"]
                    ),
                    "migration_type": group["mode"],
                    "table_mappings": build_table_mappings(
                        group["tables"]
                    ),
                },
            },
        }

        groups[group_name] = {
            "mode": group["mode"],
            "tables": copy.deepcopy(group["tables"]),
            "landing": {
                "bucket_name": destination["bucket_name"],
                "prefix": bucket_folder + "/",
            },
            "deployment_name": names["name"],
            "replication_task_id": (
                names["replication_task_id"]
            ),
        }

    runtime_configuration = {
        "configuration_version": (
            service["configuration_version"]
        ),
        "configuration_revision": configuration_revision,
        "environment": bindings["environment"],
        "region": bindings["region"],
        "source": service["service"],
        "parent_domain": service["parent_domain"],
        "source_endpoint": copy.deepcopy(
            bindings["source_endpoint"]
        ),
        "concurrency": copy.deepcopy(
            bindings["concurrency"]
        ),
        "schedule": copy.deepcopy(
            service["schedule"]
        ),
        "extraction_generation": extraction_generation,
        "task_groups": groups,
    }

    artifact = {
        "artifact_version": ARTIFACT_VERSION,
        "compiler_version": COMPILER_VERSION,
        "naming_version": NAMING_VERSION,
        "configuration_revision": configuration_revision,
        "extraction_generation": extraction_generation,
        "runtime_configuration": runtime_configuration,
        "module_inputs": module_inputs,
    }

    artifact["artifact_revision"] = fingerprint(artifact)

    return artifact