from datetime import datetime
from typing import Any


class InspectionError(RuntimeError):
    """DMS did not return the expected task or table information."""


class TaskInspection:
    def __init__(self, dms_client: Any) -> None:
        self.dms = dms_client

    @staticmethod
    def _expected(
        binding: dict[str, Any],
    ) -> dict[str, str]:
        try:
            resources = binding["resources"]

            expected = {
                "ReplicationTaskArn": resources[
                    "replication_task_arn"
                ],
                "ReplicationInstanceArn": resources[
                    "replication_instance_arn"
                ],
                "SourceEndpointArn": resources["source_endpoint_arn"],
                "TargetEndpointArn": resources["target_endpoint_arn"],
                "MigrationType": binding["task_group"]["migration_type"],
            }

        except (KeyError, TypeError) as error:
            raise ValueError(
                "A complete resolved task binding is required"
            ) from error

        if not all(
            isinstance(value, str)
            and value
            and value == value.strip()
            for value in expected.values()
        ):
            raise ValueError(
                "Task binding values must be non-empty trimmed strings"
            )

        if expected["MigrationType"] not in {
            "full-load",
            "full-load-and-cdc",
            "cdc",
        }:
            raise ValueError(
                "Unsupported task binding migration_type"
            )

        return expected

    def task_status(
        self,
        binding: dict[str, Any],
    ) -> dict[str, Any]:
        expected = self._expected(binding)

        response = self.dms.describe_replication_tasks(
            Filters=[
                {
                    "Name": "replication-task-arn",
                    "Values": [expected["ReplicationTaskArn"]],
                }
            ],
            WithoutSettings=True,
        )

        tasks = response.get("ReplicationTasks", [])

        if len(tasks) != 1 or response.get("Marker"):
            raise InspectionError(
                "DMS must return exactly one replication task"
            )

        task = tasks[0]

        for field, value in expected.items():
            if task.get(field) != value:
                raise InspectionError(
                    f"DMS task {field} does not match the pinned binding"
                )

        status = task.get("Status")

        if not isinstance(status, str) or not status:
            raise InspectionError(
                "DMS returned no replication task status"
            )

        stats = task.get("ReplicationTaskStats") or {}

        return {
            "replication_task_arn": expected["ReplicationTaskArn"],
            "migration_type": expected["MigrationType"],
            "status": status,
            "stop_reason": task.get("StopReason"),
            "has_started": bool(
                task.get("ReplicationTaskStartDate")
                or task.get("RecoveryCheckpoint")
                or stats.get("StartDate")
                or stats.get("FreshStartDate")
            ),
            "full_load_progress_percent": stats.get(
                "FullLoadProgressPercent"
            ),
            "tables_loaded": stats.get("TablesLoaded"),
            "tables_loading": stats.get("TablesLoading"),
            "tables_queued": stats.get("TablesQueued"),
            "tables_errored": stats.get("TablesErrored"),
        }

    def table_status_page(
        self,
        binding: dict[str, Any],
        *,
        marker: str | None = None,
    ) -> dict[str, Any]:
        expected = self._expected(binding)

        if marker is not None and (
            not isinstance(marker, str)
            or not marker
            or marker != marker.strip()
        ):
            raise ValueError(
                "marker must be a non-empty trimmed string"
            )

        arguments = {
            "ReplicationTaskArn": expected["ReplicationTaskArn"],
            "MaxRecords": 100,
        }

        if marker is not None:
            arguments["Marker"] = marker

        response = self.dms.describe_table_statistics(**arguments)

        if (
            response.get("ReplicationTaskArn")
            != expected["ReplicationTaskArn"]
        ):
            raise InspectionError(
                "DMS table statistics belong to a different task"
            )

        next_marker = response.get("Marker") or None

        if next_marker is not None and (
            not isinstance(next_marker, str)
            or next_marker == marker
        ):
            raise InspectionError(
                "DMS returned an invalid or repeated table marker"
            )

        tables = []
        identities = set()

        for record in response.get("TableStatistics", []):
            identity = (
                record.get("SchemaName"),
                record.get("TableName"),
            )

            if not all(
                isinstance(value, str) and value
                for value in identity
            ):
                raise InspectionError(
                    "DMS returned a table without its identity"
                )

            if identity in identities:
                raise InspectionError(
                    "DMS returned duplicate table statistics"
                )

            identities.add(identity)

            tables.append(
                {
                    "schema_name": identity[0],
                    "table_name": identity[1],
                    "state": record.get("TableState"),
                    "full_load_rows": record.get("FullLoadRows"),
                    "full_load_reloaded": record.get("FullLoadReloaded"),
                    "full_load_started_at": self._timestamp(
                        record.get("FullLoadStartTime")
                    ),
                    "full_load_finished_at": self._timestamp(
                        record.get("FullLoadEndTime")
                    ),
                }
            )

        return {
            "replication_task_arn": expected["ReplicationTaskArn"],
            "tables": tables,
            "next_marker": next_marker,
        }

    @staticmethod
    def _timestamp(value: Any) -> str | None:
        if value is None:
            return None

        if (
            not isinstance(value, datetime)
            or value.utcoffset() is None
        ):
            raise InspectionError(
                "DMS timestamps must be timezone-aware datetimes"
            )

        return value.isoformat()