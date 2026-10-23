import hashlib
import json
import re
from dataclasses import dataclass


SLUG_PATTERN = re.compile(
    r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*"
)
IDENTIFIER_PATTERN = re.compile(
    r"[A-Za-z_][A-Za-z0-9_$#]{0,127}"
)


def validate_slug(value: str, field: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) > 63
        or SLUG_PATTERN.fullmatch(value) is None
    ):
        raise ValueError(
            f"{field} must be a lowercase identifier containing "
            "letters, numbers and single separating hyphens"
        )


def validate_database_identifier(
    value: str,
    field: str,
) -> None:
    if (
        not isinstance(value, str)
        or IDENTIFIER_PATTERN.fullmatch(value) is None
    ):
        raise ValueError(
            f"{field} must match "
            "[A-Za-z_][A-Za-z0-9_$#]{0,127}"
        )


def identity_digest(parts: list[str]) -> str:
    encoded = json.dumps(
        parts,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class TableIdentity:
    environment: str
    source: str
    database_name: str
    schema_name: str
    table_name: str

    def __post_init__(self) -> None:
        validate_slug(self.environment, "environment")
        validate_slug(self.source, "source")

        for field in (
            "database_name",
            "schema_name",
            "table_name",
        ):
            validate_database_identifier(
                getattr(self, field),
                field,
            )

    @property
    def partition_key(self) -> str:
        digest = identity_digest(
            [
                "table-v1",
                self.environment,
                self.source,
                self.database_name,
                self.schema_name,
                self.table_name,
            ]
        )

        return f"TABLE#{digest}"


@dataclass(frozen=True)
class TaskGroupIdentity:
    environment: str
    source: str
    database_name: str
    task_group: str

    def __post_init__(self) -> None:
        validate_slug(self.environment, "environment")
        validate_slug(self.source, "source")
        validate_slug(self.task_group, "task_group")
        validate_database_identifier(
            self.database_name,
            "database_name",
        )

    @property
    def partition_key(self) -> str:
        digest = identity_digest(
            [
                "task-group-v1",
                self.environment,
                self.source,
                self.database_name,
                self.task_group,
            ]
        )

        return f"TASKGROUP#{digest}"


def request_partition_key(
    *,
    environment: str,
    source: str,
    request_id: str,
) -> str:
    validate_slug(environment, "environment")
    validate_slug(source, "source")

    if (
        not isinstance(request_id, str)
        or not request_id
        or request_id != request_id.strip()
        or len(request_id.encode("utf-8")) > 256
    ):
        raise ValueError(
            "request_id must contain 1 to 256 UTF-8 bytes "
            "without surrounding whitespace"
        )

    digest = identity_digest(
        [
            "request-v1",
            environment,
            source,
            request_id,
        ]
    )

    return f"REQUEST#{digest}"