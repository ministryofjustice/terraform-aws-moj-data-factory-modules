import copy
import re
from pathlib import Path
from typing import Any

from ingestion_config.loader import (
    ConfigurationError,
    load_document,
    load_service,
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigurationError(message)


def check_arn(
    value: str,
    service: str,
    region: str,
    label: str,
) -> None:
    resource_pattern = {
        "secretsmanager": r"secret:[A-Za-z0-9/_+=.@-]+",
        "kms": r"key/[0-9a-fA-F-]{36}",
    }[service]

    pattern = (
        rf"arn:aws:{service}:{re.escape(region)}:"
        rf"[0-9]{{12}}:{resource_pattern}"
    )

    require(
        re.fullmatch(pattern, value) is not None,
        f"{label}: expected a {service} ARN in {region}",
    )


def load_platform(path: str | Path) -> dict[str, Any]:
    document = load_document(
        path,
        "platform-v1.schema.json",
    )

    region = document["region"]

    for name, network in document["network_profiles"].items():
        identifiers = (
            ("vpc_id", [network["vpc_id"]], "vpc"),
            ("subnet_ids", network["subnet_ids"], "subnet"),
            (
                "additional_security_group_ids",
                network["additional_security_group_ids"],
                "sg",
            ),
        )

        for field, values, prefix in identifiers:
            for value in values:
                pattern = (
                    rf"{prefix}-"
                    rf"(?:[0-9a-f]{{8}}|[0-9a-f]{{17}})"
                )

                require(
                    re.fullmatch(pattern, value) is not None,
                    f"network_profiles.{name}.{field}: "
                    f"invalid {prefix} identifier",
                )

    for name, profile in document["replication_profiles"].items():
        if "kms_key_arn" in profile:
            check_arn(
                profile["kms_key_arn"],
                "kms",
                region,
                f"replication_profiles.{name}.kms_key_arn",
            )

    for name, destination in document["destination_profiles"].items():
        bucket = destination["bucket_name"]

        require(
            re.fullmatch(
                r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]",
                bucket,
            ) is not None
            and ".." not in bucket
            and ".-" not in bucket
            and "-." not in bucket
            and re.fullmatch(
                r"[0-9]+(?:\.[0-9]+){3}",
                bucket,
            ) is None,
            f"destination_profiles.{name}.bucket_name: "
            "invalid bucket name",
        )

        prefix_parts = (
            destination["landing_prefix"].split("/")[:-1]
        )

        require(
            all(
                part not in ("", ".", "..")
                for part in prefix_parts
            ),
            f"destination_profiles.{name}.landing_prefix: "
            "empty or relative path segments are unsupported",
        )

        check_arn(
            destination["kms_key_arn"],
            "kms",
            region,
            f"destination_profiles.{name}.kms_key_arn",
        )

    for name, limits in document["concurrency_profiles"].items():
        require(
            limits["max_parallel_full_loads"]
            <= limits["max_parallel_tables"],
            f"concurrency_profiles.{name}: "
            "full-load limit must not exceed table limit",
        )

    for name, binding in document["services"].items():
        for reference, collection in (
            ("network_profile", "network_profiles"),
            ("replication_profile", "replication_profiles"),
        ):
            require(
                binding[reference] in document[collection],
                f"services.{name}.{reference}: "
                f"unknown profile {binding[reference]!r}",
            )

        for reference, collection in (
            (
                "allowed_destination_profiles",
                "destination_profiles",
            ),
            (
                "allowed_concurrency_profiles",
                "concurrency_profiles",
            ),
        ):
            for profile in binding[reference]:
                require(
                    profile in document[collection],
                    f"services.{name}.{reference}: "
                    f"unknown profile {profile!r}",
                )

        check_arn(
            binding["secret_arn"],
            "secretsmanager",
            region,
            f"services.{name}.secret_arn",
        )

        if "secret_kms_key_arn" in binding:
            check_arn(
                binding["secret_kms_key_arn"],
                "kms",
                region,
                f"services.{name}.secret_kms_key_arn",
            )

    return document


def resolve_service(
    service_path: str | Path,
    platform_path: str | Path,
) -> dict[str, Any]:
    service = load_service(service_path)
    platform = load_platform(platform_path)

    name = service["service"]

    require(
        name in platform["services"],
        f"service {name!r}: no platform binding",
    )

    binding = platform["services"][name]

    for field in ("engine", "database_name", "secret_ref"):
        require(
            service["source"][field] == binding[field],
            f"source.{field}: does not match "
            f"the authorised binding for {name!r}",
        )

    for field, allowed in (
        (
            "destination_profile",
            "allowed_destination_profiles",
        ),
        (
            "concurrency_profile",
            "allowed_concurrency_profiles",
        ),
    ):
        require(
            service[field] in binding[allowed],
            f"{field}: {service[field]!r} "
            f"is not authorised for {name!r}",
        )

    destination = platform["destination_profiles"][
        service["destination_profile"]
    ]

    require(
        service["parent_domain"] == destination["parent_domain"],
        "parent_domain: does not match "
        "the authorised destination",
    )

    for group_name, group in service["task_groups"].items():
        for index, table in enumerate(group["tables"]):
            for component in (
                "processing",
                "history",
                "publication",
            ):
                field = f"{component}_profile"

                require(
                    table[field]
                    in platform["component_profiles"][component],
                    f"task_groups.{group_name}.tables."
                    f"{index}.{field}: "
                    f"unknown profile {table[field]!r}",
                )

    network = copy.deepcopy(
        platform["network_profiles"][
            binding["network_profile"]
        ]
    )

    vpc_id = network.pop("vpc_id")

    source_endpoint = {
        "engine": binding["engine"],
        "database_name": binding["database_name"],
        "secret_arn": binding["secret_arn"],
    }

    if "secret_kms_key_arn" in binding:
        source_endpoint["secret_kms_key_arn"] = binding[
            "secret_kms_key_arn"
        ]

    for field in (
        "ssl_mode",
        "certificate_arn",
        "extra_connection_attributes",
    ):
        if field in binding:
            source_endpoint[field] = binding[field]

    return {
        "service_configuration": copy.deepcopy(service),
        "bindings": {
            "environment": platform["environment"],
            "region": platform["region"],
            "vpc_id": vpc_id,
            "network": network,
            "replication_instance": copy.deepcopy(
                platform["replication_profiles"][
                    binding["replication_profile"]
                ]
            ),
            "source_endpoint": source_endpoint,
            "destination": copy.deepcopy(destination),
            "concurrency": copy.deepcopy(
                platform["concurrency_profiles"][
                    service["concurrency_profile"]
                ]
            ),
        },
    }