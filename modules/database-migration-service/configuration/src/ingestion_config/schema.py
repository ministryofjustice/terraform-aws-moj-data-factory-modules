import json
from importlib.resources import files
from typing import Any


SCHEMA_NAMES = frozenset(
    {
        "service-v1.schema.json",
        "platform-v1.schema.json",
    }
)


def load_schema(name: str) -> dict[str, Any]:
    if name not in SCHEMA_NAMES:
        raise ValueError(
            f"Unsupported bundled schema: {name}"
        )

    resource = (
        files("ingestion_config")
        .joinpath("schemas")
        .joinpath(name)
    )

    return json.loads(
        resource.read_text(encoding="utf-8")
    )