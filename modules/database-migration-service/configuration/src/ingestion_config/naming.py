import hashlib
import json
import re

from ingestion_config.loader import ConfigurationError


SLUG = re.compile(
    r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*"
)

DATABASE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_$#]{0,127}"
)

NAMING_VERSION = 1


def build_resource_names(
    *,
    environment: str,
    region: str,
    service: str,
    database_name: str,
    task_group: str,
    extraction_generation: str,
) -> dict[str, str]:
    identity = {
        "environment": environment,
        "region": region,
        "service": service,
        "database_name": database_name,
        "task_group": task_group,
        "extraction_generation": extraction_generation,
    }

    for field in (
        "environment",
        "region",
        "service",
        "task_group",
        "extraction_generation",
    ):
        value = identity[field]

        if (
            not isinstance(value, str)
            or len(value) > 63
            or SLUG.fullmatch(value) is None
        ):
            raise ConfigurationError(
                f"{field}: expected a lowercase identifier "
                "of at most 63 characters"
            )

    if (
        not isinstance(database_name, str)
        or DATABASE.fullmatch(database_name) is None
    ):
        raise ConfigurationError(
            "database_name: unsupported database identifier"
        )

    payload = json.dumps(
        {
            "naming_version": NAMING_VERSION,
            **identity,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )

    digest = hashlib.sha256(
        payload.encode("utf-8")
    ).hexdigest()[:16]

    service_label = service[:10].rstrip("-")
    group_label = task_group[:6].rstrip("-")

    name = (
        f"df-{service_label}-{group_label}-{digest}"
    )

    # dms-source-ingestion appends this suffix to its name input.
    instance_id = (
        f"{name}-dms-instance-{environment}"
    )

    if len(instance_id) > 63:
        raise ConfigurationError(
            "Generated replication instance ID exceeds "
            "63 characters; the environment identifier "
            "is too long for naming version 1"
        )

    return {
        "name": name,
        "replication_task_id": f"{name}-task",
    }