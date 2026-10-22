import json
from pathlib import Path
from typing import Any

from ingestion_config.loader import ConfigurationError


MAX_JSON_BYTES = 4 * 1024 * 1024


def reject_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result = {}

    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")

        result[key] = value

    return result


def reject_non_finite(value: str) -> None:
    raise ValueError(f"Unsupported JSON constant: {value}")


def parse_json_object(
    raw: bytes,
    *,
    source: str,
) -> dict[str, Any]:
    try:
        if len(raw) > MAX_JSON_BYTES:
            raise ValueError("JSON input must not exceed 4 MiB")

        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_non_finite,
        )

        if not isinstance(document, dict):
            raise ValueError("Expected a JSON object")

        json.dumps(document, allow_nan=False)

        return document

    except (
        UnicodeError,
        ValueError,
        RecursionError,
    ) as error:
        raise ConfigurationError(
            f"{source}: {error}"
        ) from error


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_JSON_BYTES + 1)
    except OSError as error:
        raise ConfigurationError(
            f"{path}: {error}"
        ) from error

    return parse_json_object(raw, source=str(path))