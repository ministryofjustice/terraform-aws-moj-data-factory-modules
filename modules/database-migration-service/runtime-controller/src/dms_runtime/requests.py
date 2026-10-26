import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ingestion_registry.identity import validate_database_identifier


READ_ACTIONS = frozenset(
    {
        "task_status",
        "table_status",
        "full_load_status",
        "operation_status",
    }
)

OWNED_ACTIONS = frozenset(
    {
        "preflight",
        "start",
        "resume",
        "stop",
    }
)

MAX_REQUEST_BYTES = 16384


def _identifier(name: str, value: Any) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 256
    ):
        raise ValueError(
            f"{name} must contain 1 to 256 UTF-8 bytes "
            "without surrounding whitespace"
        )


def _positive_integer(name: str, value: Any) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 1
    ):
        raise ValueError(
            f"{name} must be a positive integer"
        )


@dataclass(frozen=True)
class IngestionOwner:
    execution_id: str
    fencing_token: int
    attempt: int

    def __post_init__(self) -> None:
        _identifier("execution_id", self.execution_id)
        _positive_integer("fencing_token", self.fencing_token)
        _positive_integer("attempt", self.attempt)


@dataclass(frozen=True)
class TaskOwner:
    execution_id: str
    group_fencing_token: int
    task_fencing_token: int

    def __post_init__(self) -> None:
        _identifier("execution_id", self.execution_id)
        _positive_integer(
            "group_fencing_token",
            self.group_fencing_token,
        )
        _positive_integer(
            "task_fencing_token",
            self.task_fencing_token,
        )


@dataclass(frozen=True)
class SelectedTable:
    schema_name: str
    table_name: str

    def __post_init__(self) -> None:
        validate_database_identifier(
            self.schema_name,
            "schema_name",
        )
        validate_database_identifier(
            self.table_name,
            "table_name",
        )


@dataclass(frozen=True)
class RuntimeRequest:
    action: str
    ingestion_id: str
    request_version: int = 1
    operation_id: str | None = None
    ingestion_owner: IngestionOwner | None = None
    task_owner: TaskOwner | None = None
    marker: str | None = None
    selected_tables: tuple[SelectedTable, ...] | None = None
    start_operation_id: str | None = None
    target_operation_id: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.request_version) is not int
            or self.request_version != 1
        ):
            raise ValueError("request_version must be 1")

        if self.action == "recover_table":
            raise ValueError(
                "Table recovery requires a separately "
                "supported recovery strategy"
            )

        if (
            not isinstance(self.action, str)
            or self.action not in READ_ACTIONS | OWNED_ACTIONS
        ):
            raise ValueError("Unsupported runtime action")

        _identifier("ingestion_id", self.ingestion_id)

        if self.action in OWNED_ACTIONS:
            _identifier("operation_id", self.operation_id)

            if (
                not isinstance(self.ingestion_owner, IngestionOwner)
                or not isinstance(self.task_owner, TaskOwner)
            ):
                raise ValueError(
                    "This operation requires "
                    "ingestion_owner and task_owner"
                )

        elif any(
            value is not None
            for value in (
                self.operation_id,
                self.ingestion_owner,
                self.task_owner,
            )
        ):
            raise ValueError(
                "Read operations must not supply operation ownership"
            )

        if self.marker is not None:
            if (
                self.action != "table_status"
                or not isinstance(self.marker, str)
                or not self.marker
                or self.marker != self.marker.strip()
            ):
                raise ValueError(
                    "marker is only supported as "
                    "a non-empty table_status cursor"
                )

        if self.action == "full_load_status":
            _identifier(
                "start_operation_id",
                self.start_operation_id,
            )

        elif self.start_operation_id is not None:
            raise ValueError(
                "start_operation_id is only supported "
                "for full_load_status"
            )

        if self.action == "operation_status":
            _identifier(
                "target_operation_id",
                self.target_operation_id,
            )

        elif self.target_operation_id is not None:
            raise ValueError(
                "target_operation_id is only supported "
                "for operation_status"
            )

        if self.selected_tables is not None:
            if (
                self.action != "full_load_status"
                or not isinstance(self.selected_tables, tuple)
                or not self.selected_tables
                or not all(
                    isinstance(table, SelectedTable)
                    for table in self.selected_tables
                )
                or len(set(self.selected_tables))
                != len(self.selected_tables)
            ):
                raise ValueError(
                    "selected_tables must be a non-empty "
                    "unique Full Load selection"
                )

    @classmethod
    def from_event(cls, event: Any) -> "RuntimeRequest":
        if (
            not isinstance(event, dict)
            or not all(isinstance(key, str) for key in event)
        ):
            raise ValueError(
                "Expected a runtime request object"
            )

        allowed = {
            "request_version",
            "action",
            "ingestion_id",
            "operation_id",
            "ingestion_owner",
            "task_owner",
            "marker",
            "selected_tables",
            "start_operation_id",
            "target_operation_id",
        }

        if set(event) - allowed:
            raise ValueError(
                "Unsupported request fields: "
                + ", ".join(sorted(set(event) - allowed))
            )

        if not {
            "request_version",
            "action",
            "ingestion_id",
        } <= set(event):
            raise ValueError(
                "request_version, action and ingestion_id are required"
            )

        try:
            encoded = json.dumps(
                event,
                allow_nan=False,
            ).encode("utf-8")

        except (TypeError, ValueError) as error:
            raise ValueError(
                "Runtime request must contain valid JSON values"
            ) from error

        if len(encoded) > MAX_REQUEST_BYTES:
            raise ValueError(
                "Runtime request must not exceed 16 KiB; "
                "keep bulk configuration outside the request"
            )

        values = dict(event)

        for field, model, fields in (
            (
                "ingestion_owner",
                IngestionOwner,
                {
                    "execution_id",
                    "fencing_token",
                    "attempt",
                },
            ),
            (
                "task_owner",
                TaskOwner,
                {
                    "execution_id",
                    "group_fencing_token",
                    "task_fencing_token",
                },
            ),
        ):
            value = values.get(field)

            if value is not None:
                if (
                    not isinstance(value, dict)
                    or set(value) != fields
                ):
                    raise ValueError(
                        f"Invalid {field} fields"
                    )

                values[field] = model(**value)

        tables = values.get("selected_tables")

        if tables is not None:
            if (
                not isinstance(tables, list)
                or not all(
                    isinstance(table, dict)
                    and set(table) == {
                        "schema_name",
                        "table_name",
                    }
                    for table in tables
                )
            ):
                raise ValueError(
                    "selected_tables must contain explicit "
                    "schema_name and table_name objects"
                )

            values["selected_tables"] = tuple(
                SelectedTable(**table)
                for table in tables
            )

        return cls(**values)

    @property
    def operation_fingerprint(self) -> str:
        if self.action not in OWNED_ACTIONS:
            raise ValueError(
                "Read operations do not have an operation fingerprint"
            )

        payload = {
            "request_version": self.request_version,
            "action": self.action,
            "ingestion_id": self.ingestion_id,
            "operation_id": self.operation_id,
        }

        return hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()