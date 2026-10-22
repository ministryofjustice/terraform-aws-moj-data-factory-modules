import copy
import re
from typing import Any

from ingestion_config.loader import ConfigurationError


PROFILE_FIELDS = {
    "processing": "processing_profile",
    "raw_history": "history_profile",
    "publication": "publication_profile",
}

RESOURCE_TYPES = {
    "lambda": ("lambda", "function"),
    "state_machine": ("states", "stateMachine"),
}


def resolve_component_bindings(
    table_binding: dict[str, Any],
    catalog: dict[str, Any],
    *,
    required_components: list[str],
) -> dict[str, Any]:
    try:
        return _resolve(
            table_binding,
            catalog,
            required_components=required_components,
        )
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        if isinstance(error, ConfigurationError):
            raise

        raise ConfigurationError(
            "Component lookup requires a valid table binding "
            "and deployment component catalog"
        ) from error


def _resolve(
    table_binding: dict[str, Any],
    catalog: dict[str, Any],
    *,
    required_components: list[str],
) -> dict[str, Any]:
    if catalog["catalog_version"] != 1:
        raise ConfigurationError(
            "Unsupported component catalog version"
        )

    for field in (
        "environment",
        "account_id",
        "region",
        "configuration_revision",
        "artifact_revision",
    ):
        if catalog[field] != table_binding[field]:
            raise ConfigurationError(
                f"Component catalog {field} does not match "
                "the pinned table binding"
            )

    if len(required_components) != len(set(required_components)):
        raise ConfigurationError(
            "Required component names must not contain duplicates"
        )

    resolved = {}

    for component in required_components:
        if component not in PROFILE_FIELDS:
            raise ConfigurationError(
                f"Unsupported downstream component: {component!r}"
            )

        profile_field = PROFILE_FIELDS[component]
        profile = table_binding["table"][profile_field]

        profiles = catalog["components"].get(component, {})

        if profile not in profiles:
            raise ConfigurationError(
                f"Required component {component!r} has no deployed "
                f"binding for profile {profile!r}"
            )

        target = profiles[profile]

        if not isinstance(target, dict) or set(target) != {
            "kind",
            "arn",
        }:
            raise ConfigurationError(
                f"Component {component!r} profile {profile!r} "
                "must contain exactly kind and arn"
            )

        kind = target["kind"]

        if kind not in RESOURCE_TYPES:
            raise ConfigurationError(
                f"Unsupported resource kind for {component!r}: "
                f"{kind!r}"
            )

        service, resource_type = RESOURCE_TYPES[kind]

        prefix = (
            f"arn:aws:{service}:{table_binding['region']}:"
            f"{table_binding['account_id']}:{resource_type}:"
        )

        arn = target["arn"]

        if (
            not isinstance(arn, str)
            or not arn.startswith(prefix)
            or not re.fullmatch(
                r"[A-Za-z0-9_-]+(?::[A-Za-z0-9_-]+)?",
                arn[len(prefix):],
            )
        ):
            raise ConfigurationError(
                f"Component {component!r} ARN must identify "
                "the expected resource kind in the pinned "
                "account and region"
            )

        resolved[component] = {
            "profile": profile,
            "kind": kind,
            "arn": arn,
        }

    return copy.deepcopy(resolved)