import hashlib
import json
import re
from typing import Any

from ingestion_config.loader import ConfigurationError


IDENTIFIER_PATTERN = re.compile(
    r"[A-Za-z_][A-Za-z0-9_$#]{0,127}"
)


def build_table_mappings(
    tables: list[dict[str, Any]],
) -> str:
    if not isinstance(tables, list) or not tables:
        raise ConfigurationError(
            "Table mappings require a non-empty table list"
        )

    identities: set[tuple[str, str]] = set()

    for index, table in enumerate(tables):
        if not isinstance(table, dict):
            raise ConfigurationError(
                f"tables.{index}: expected a table object"
            )

        names = []

        for field in ("schema_name", "table_name"):
            value = table.get(field)

            if (
                not isinstance(value, str)
                or IDENTIFIER_PATTERN.fullmatch(value) is None
            ):
                raise ConfigurationError(
                    f"tables.{index}.{field}: "
                    "unsupported identifier; provide an exact "
                    "supported name without wildcard patterns"
                )

            names.append(value)

        identity = (names[0], names[1])

        if identity in identities:
            raise ConfigurationError(
                "Duplicate table in mappings: "
                f"{identity[0]}.{identity[1]}"
            )

        identities.add(identity)

    rules = []
    rule_ids: set[str] = set()

    for schema_name, table_name in sorted(identities):
        identity_json = json.dumps(
            [schema_name, table_name],
            separators=(",", ":"),
            ensure_ascii=False,
        )

        digest = hashlib.sha256(
            identity_json.encode("utf-8")
        ).hexdigest()

        rule_id = str(
            int(digest[:8], 16) % 2_147_483_647 + 1
        )

        if rule_id in rule_ids:
            raise ConfigurationError(
                "Generated DMS rule ID collision; "
                "mappings were not produced"
            )

        rule_ids.add(rule_id)

        rules.append(
            {
                "rule-type": "selection",
                "rule-id": rule_id,
                "rule-name": "table" + digest[:16],
                "rule-action": "explicit",
                "object-locator": {
                    "schema-name": schema_name,
                    "table-name": table_name,
                    "table-type": "table",
                },
            }
        )

    return json.dumps(
        {"rules": rules},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )