import json
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from yaml.constructor import ConstructorError
from yaml.tokens import AliasToken, AnchorToken
from ingestion_config.scheduling import validate_schedule
from ingestion_config.schema import load_schema


class ConfigurationError(ValueError):
    """Configuration cannot be used for compilation."""


class StrictLoader(yaml.SafeLoader):
    pass


def construct_mapping(
    loader: StrictLoader,
    node: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {}

    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)

        if not isinstance(key, str) or key == "<<":
            raise ConstructorError(
                None,
                None,
                "Keys must be strings; YAML merge keys are unsupported",
                key_node.start_mark,
            )

        if key in result:
            raise ConstructorError(
                None,
                None,
                f"Duplicate key: {key}",
                key_node.start_mark,
            )

        result[key] = loader.construct_object(
            value_node,
            deep=True,
        )

    return result


StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    construct_mapping,
)


def load_document(
    path: str | Path,
    schema_name: str,
) -> dict[str, Any]:
    source = Path(path)

    try:
        with source.open("rb") as stream:
            raw = stream.read(1_048_577)

        if len(raw) > 1_048_576:
            raise ConfigurationError(
                "Configuration YAML must not exceed 1 MiB"
            )

        text = raw.decode("utf-8")

        for token in yaml.scan(text):
            if isinstance(token, (AliasToken, AnchorToken)):
                raise ConfigurationError(
                    "YAML anchors and aliases are unsupported; "
                    "use explicit configuration"
                )

        document = yaml.load(text, Loader=StrictLoader)

        # Reject values that cannot be represented in the compiled JSON.
        json.dumps(document, allow_nan=False)

    except (
        OSError,
        UnicodeError,
        yaml.YAMLError,
        TypeError,
        ValueError,
        RecursionError,
    ) as error:
        raise ConfigurationError(
            f"{source}: {error}"
        ) from error

    schema = load_schema(schema_name)

    Draft202012Validator.check_schema(schema)

    errors = sorted(
        Draft202012Validator(schema).iter_errors(document),
        key=lambda error: tuple(
            str(part) for part in error.absolute_path
        ),
    )

    if errors:
        details = []

        for error in errors:
            location = ".".join(
                str(part) for part in error.absolute_path
            ) or "<root>"

            details.append(
                f"{location}: {error.message}"
            )

        raise ConfigurationError(
            f"{source}:\n" + "\n".join(details)
        )

    return document


def load_service(path: str | Path) -> dict[str, Any]:
    source = Path(path)

    document = load_document(
        source,
        "service-v1.schema.json",
    )

    seen: dict[tuple[str, str], str] = {}

    for group_name, group in document["task_groups"].items():
        for table in group["tables"]:
            identity = (
                table["schema_name"],
                table["table_name"],
            )

            if identity in seen:
                raise ConfigurationError(
                    f"{source}: duplicate table "
                    f"{identity[0]}.{identity[1]} "
                    f"in task groups {seen[identity]!r} "
                    f"and {group_name!r}"
                )

            seen[identity] = group_name

    try:
        validate_schedule(document)
    except ValueError as error:
        raise ConfigurationError(
            f"{source}: {error}"
        ) from error

    return document