from datetime import datetime
from typing import Any

from dms_runtime.inspection import (
    InspectionError,
    TaskInspection,
)


class FullLoadReadiness:
    def __init__(self, inspection: TaskInspection) -> None:
        self.inspection = inspection

    @staticmethod
    def _identities(
        tables: Any,
    ) -> list[tuple[str, str]]:
        if not isinstance(tables, list) or not tables:
            raise ValueError(
                "A non-empty explicit table selection is required"
            )

        identities = []
        seen = set()

        for table in tables:
            if not isinstance(table, dict):
                raise ValueError(
                    "Each selected table must be an object"
                )

            identity = (
                table.get("schema_name"),
                table.get("table_name"),
            )

            if not all(
                isinstance(value, str)
                and value
                and value == value.strip()
                for value in identity
            ):
                raise ValueError(
                    "Each selected table needs schema_name "
                    "and table_name"
                )

            if identity in seen:
                raise ValueError("Duplicate selected table")

            seen.add(identity)
            identities.append(identity)

        return sorted(identities)

    def assess(
        self,
        binding: dict[str, Any],
        *,
        full_load_not_before: datetime,
        selected_tables: list[dict[str, str]] | None = None,
        max_pages: int = 10,
    ) -> dict[str, Any]:
        if (
            not isinstance(full_load_not_before, datetime)
            or full_load_not_before.utcoffset() is None
        ):
            raise ValueError(
                "full_load_not_before must be timezone-aware"
            )

        if (
            not isinstance(max_pages, int)
            or isinstance(max_pages, bool)
            or not 1 <= max_pages <= 100
        ):
            raise ValueError(
                "max_pages must be an integer from 1 to 100"
            )

        try:
            group = binding["task_group"]
            mode = group["migration_type"]
            configured = self._identities(group["tables"])

        except (KeyError, TypeError) as error:
            raise ValueError(
                "A resolved task-group binding is required"
            ) from error

        if mode not in {"full-load", "full-load-and-cdc"}:
            raise ValueError(
                "Full Load readiness is unsupported "
                "for this migration type"
            )

        selected = (
            configured
            if selected_tables is None
            else self._identities(selected_tables)
        )
        selected_set = set(selected)

        if not selected_set <= set(configured):
            raise ValueError(
                "Requested table is not selected "
                "in the pinned task group"
            )

        task = self.inspection.task_status(binding)

        observed = {}
        marker = None
        markers = set()

        for _ in range(max_pages):
            page = self.inspection.table_status_page(
                binding,
                marker=marker,
            )

            if (
                page["replication_task_arn"]
                != task["replication_task_arn"]
            ):
                raise InspectionError(
                    "Table page does not match the inspected task"
                )

            for table in page["tables"]:
                identity = (
                    table["schema_name"],
                    table["table_name"],
                )

                if identity not in selected_set:
                    continue

                if identity in observed:
                    raise InspectionError(
                        "Selected table appeared on multiple "
                        "statistics pages"
                    )

                observed[identity] = table

            marker = page["next_marker"]

            if marker is None:
                break

            if marker in markers:
                raise InspectionError(
                    "DMS table pagination contains a marker cycle"
                )

            markers.add(marker)

        else:
            raise InspectionError(
                "Table statistics page budget exhausted; "
                "readiness was not determined"
            )

        results = []

        for schema, name in selected:
            table = observed.get((schema, name))
            status, reason = self._table_status(
                table,
                full_load_not_before,
            )

            results.append(
                {
                    "schema_name": schema,
                    "table_name": name,
                    "status": status,
                    "reason": reason,
                    "state": table.get("state") if table else None,
                }
            )

        statuses = {table["status"] for table in results}
        task_status = task["status"]

        if "FAILED" in statuses:
            overall = "FAILED"
        elif "BLOCKED" in statuses:
            overall = "BLOCKED"
        elif "WAITING" in statuses:
            if task_status in {"failed", "failed-move"}:
                overall = "FAILED"
            elif task_status in {"stopped", "stopping", "deleting"}:
                overall = "BLOCKED"
            else:
                overall = "WAITING"
        else:
            overall = "SUCCEEDED"

        return {
            "replication_task_arn": task["replication_task_arn"],
            "full_load_status": overall,
            "full_load_ready": overall == "SUCCEEDED",
            "full_load_not_before": full_load_not_before.isoformat(),
            "task_status": task_status,
            "task_requires_attention": (
                task_status in {"failed", "failed-move"}
                or (task.get("tables_errored") or 0) > 0
            ),
            "tables": results,
        }

    @staticmethod
    def _table_status(
        table: dict[str, Any] | None,
        not_before: datetime,
    ) -> tuple[str, str]:
        if table is None:
            return "WAITING", "STATISTICS_NOT_AVAILABLE"

        state = table.get("state")

        if state in {
            "Table error",
            "Table cancelled",
            "Table does not exist",
        }:
            return "FAILED", "TABLE_LOAD_FAILED"

        if state in {
            "Before load",
            "Full load",
            "Table is being reloaded",
        }:
            return "WAITING", "TABLE_LOAD_IN_PROGRESS"

        if state != "Table completed":
            return "BLOCKED", "UNSUPPORTED_TABLE_STATE"

        started = table.get("full_load_started_at")
        finished = table.get("full_load_finished_at")

        if started is None or finished is None:
            return "WAITING", "LOAD_TIMES_NOT_AVAILABLE"

        try:
            started = datetime.fromisoformat(started)
            finished = datetime.fromisoformat(finished)

            if (
                started.utcoffset() is None
                or finished.utcoffset() is None
            ):
                raise ValueError("Naive timestamp")

        except (TypeError, ValueError) as error:
            raise InspectionError(
                "Invalid table Full Load timestamps"
            ) from error

        if finished < started:
            raise InspectionError(
                "Table Full Load finished before it started"
            )

        if started < not_before:
            return "WAITING", "STATISTICS_FROM_EARLIER_LOAD"

        return "SUCCEEDED", "TABLE_LOAD_COMPLETED"