import copy
import re
from typing import Any

from ingestion_config.loader import ConfigurationError


CAPABILITIES = frozenset(
    {
        "generation",
        "validation",
        "registration",
        "registry",
    }
)


def resolve_contract_bindings(
    table_binding: dict[str, Any],
    catalog: dict[str, Any],
    *,
    required_capabilities: list[str],
) -> dict[str, Any]:
    try:
        return _resolve(
            table_binding,
            catalog,
            required_capabilities=required_capabilities,
        )
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        if isinstance(error, ConfigurationError):
            raise

        raise ConfigurationError(
            "Contract lookup requires a valid table binding "
            "and deployment contract catalog"
        ) from error


def _resolve(
    binding: dict[str, Any],
    catalog: dict[str, Any],
    *,
    required_capabilities: list[str],
) -> dict[str, Any]:
    if catalog["catalog_version"] != 1:
        raise ConfigurationError("Unsupported contract catalog version")

    for field in (
        "environment",
        "account_id",
        "region",
        "configuration_revision",
        "artifact_revision",
    ):
        if catalog[field] != binding[field]:
            raise ConfigurationError(
                f"Contract catalog {field} does not match "
                "the pinned table binding"
            )

    if len(required_capabilities) != len(set(required_capabilities)):
        raise ConfigurationError(
            "Required contract capabilities must not contain duplicates"
        )

    unknown = set(required_capabilities) - CAPABILITIES

    if unknown:
        raise ConfigurationError(
            "Unsupported contract capabilities: "
            + ", ".join(sorted(unknown))
        )

    contracts = catalog["contracts"]
    table_capabilities = {}

    if set(required_capabilities) & {"generation", "validation"}:
        identity = {
            "source": binding["source"],
            "database_name": binding["database_name"],
            "schema_name": binding["table"]["schema_name"],
            "table_name": binding["table"]["table_name"],
        }

        matches = [
            entry
            for entry in contracts["tables"]
            if all(
                entry[field] == value
                for field, value in identity.items()
            )
        ]

        if len(matches) != 1:
            raise ConfigurationError(
                "Expected exactly one contract capability binding "
                "for the selected source, database, schema and table"
            )

        table_capabilities = matches[0]

    result = {}

    for capability in required_capabilities:
        owner = (
            table_capabilities
            if capability in {"generation", "validation"}
            else contracts
        )

        if capability not in owner:
            raise ConfigurationError(
                f"Required contract capability {capability!r} "
                "has no deployed binding"
            )

        target = owner[capability]

        if capability == "registry":
            _validate_registry(target, binding)
        else:
            _validate_lambda(target, binding, capability)

        result[capability] = copy.deepcopy(target)

    return result


def _validate_lambda(
    target: dict[str, Any],
    binding: dict[str, Any],
    capability: str,
) -> None:
    prefix = (
        f"arn:aws:lambda:{binding['region']}:"
        f"{binding['account_id']}:function:"
    )

    if (
        not isinstance(target, dict)
        or set(target) != {"kind", "arn"}
        or target["kind"] != "lambda"
        or not isinstance(target["arn"], str)
        or not target["arn"].startswith(prefix)
        or not re.fullmatch(
            r"[A-Za-z0-9_-]+(?::[A-Za-z0-9_-]+)?",
            target["arn"][len(prefix):],
        )
    ):
        raise ConfigurationError(
            f"Contract capability {capability!r} must reference "
            "a Lambda in the pinned account and region"
        )


def _validate_registry(
    target: dict[str, Any],
    binding: dict[str, Any],
) -> None:
    if (
        not isinstance(target, dict)
        or set(target) != {"table_name", "arn"}
        or not isinstance(target["table_name"], str)
        or not re.fullmatch(
            r"[A-Za-z0-9_.-]{3,255}",
            target["table_name"],
        )
    ):
        raise ConfigurationError(
            "Contract registry binding requires a valid table_name and arn"
        )

    expected = (
        f"arn:aws:dynamodb:{binding['region']}:"
        f"{binding['account_id']}:table/{target['table_name']}"
    )

    if target["arn"] != expected:
        raise ConfigurationError(
            "Contract registry ARN must match its table name "
            "and the pinned account and region"
        )

def resolve_contract_execution_bindings(
    table_binding: dict[str, Any],
    catalog: dict[str, Any],
    *,
    generation_function_arn: str,
    validation_function_arn: str,
    registration_function_arn: str,
    registry_table_name: str,
    registry_table_arn: str,
) -> dict[str, Any]:
    resolved = resolve_contract_bindings(
        table_binding,
        catalog,
        required_capabilities=[
            "generation",
            "validation",
            "registration",
            "registry",
        ],
    )

    for capability in (
        "generation",
        "validation",
        "registration",
    ):
        arn = resolved[capability]["arn"]

        if re.fullmatch(
            r"arn:aws:lambda:[a-z0-9-]+:[0-9]{12}:"
            r"function:[A-Za-z0-9_-]+:[1-9][0-9]*",
            arn,
        ) is None:
            raise ConfigurationError(
                f"Contract execution capability {capability!r} "
                "must reference a published numeric Lambda version"
            )

    configured = {
        "generation": {
            "kind": "lambda",
            "arn": generation_function_arn,
        },
        "validation": {
            "kind": "lambda",
            "arn": validation_function_arn,
        },
        "registration": {
            "kind": "lambda",
            "arn": registration_function_arn,
        },
        "registry": {
            "table_name": registry_table_name,
            "arn": registry_table_arn,
        },
    }

    for capability, expected in resolved.items():
        if configured[capability] != expected:
            raise ConfigurationError(
                f"Configured contract capability {capability!r} "
                "does not match the pinned deployment catalog"
            )

    return resolved