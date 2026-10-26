from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from dms_runtime.full_load import FullLoadReadiness
from dms_runtime.inspection import InspectionError


BOUNDARY = datetime(
    2026,
    10,
    26,
    9,
    tzinfo=timezone.utc,
)


def identity(name):
    return {
        "schema_name": "public",
        "table_name": name,
    }


def completed(name):
    return {
        **identity(name),
        "state": "Table completed",
        "full_load_rows": 0,
        "full_load_started_at": BOUNDARY.isoformat(),
        "full_load_finished_at": (
            BOUNDARY + timedelta(minutes=1)
        ).isoformat(),
    }


def page(tables, marker=None):
    return {
        "replication_task_arn": "task-one",
        "tables": tables,
        "next_marker": marker,
    }


@pytest.fixture
def readiness_setup():
    inspection = Mock()

    inspection.task_status.return_value = {
        "replication_task_arn": "task-one",
        "status": "running",
        "tables_errored": 0,
    }

    inspection.table_status_page.return_value = page(
        [
            completed("one"),
            completed("two"),
        ]
    )

    binding = {
        "task_group": {
            "migration_type": "full-load-and-cdc",
            "tables": [
                identity("one"),
                identity("two"),
            ],
        }
    }

    return (
        inspection,
        binding,
        FullLoadReadiness(inspection),
    )


def assess(setup, **kwargs):
    _, binding, readiness = setup

    return readiness.assess(
        binding,
        full_load_not_before=BOUNDARY,
        **kwargs,
    )


def test_all_selected_tables_complete_while_cdc_task_runs(
    readiness_setup,
):
    result = assess(readiness_setup)

    assert result["full_load_ready"] is True
    assert result["task_status"] == "running"
    assert len(result["tables"]) == 2


def test_missing_table_prevents_readiness(readiness_setup):
    inspection, _, _ = readiness_setup

    inspection.table_status_page.return_value = page(
        [completed("one")]
    )

    result = assess(readiness_setup)

    assert result["full_load_status"] == "WAITING"
    assert result["tables"][1]["reason"] == (
        "STATISTICS_NOT_AVAILABLE"
    )


def test_all_pages_are_checked(readiness_setup):
    inspection, _, _ = readiness_setup

    inspection.table_status_page.side_effect = [
        page([completed("one")], "next"),
        page([completed("two")]),
    ]

    assert assess(readiness_setup)["full_load_ready"] is True
    assert inspection.table_status_page.call_args.kwargs[
        "marker"
    ] == "next"


def test_old_statistics_are_not_accepted(readiness_setup):
    inspection, _, _ = readiness_setup

    old = completed("one")
    old["full_load_started_at"] = (
        BOUNDARY - timedelta(days=1)
    ).isoformat()

    inspection.table_status_page.return_value = page(
        [old, completed("two")]
    )

    result = assess(readiness_setup)

    assert result["full_load_ready"] is False
    assert result["tables"][0]["reason"] == (
        "STATISTICS_FROM_EARLIER_LOAD"
    )


@pytest.mark.parametrize(
    "state",
    [
        "Table error",
        "Table cancelled",
        "Table does not exist",
    ],
)
def test_table_failure_is_reported(readiness_setup, state):
    inspection, _, _ = readiness_setup

    failed = {
        **completed("two"),
        "state": state,
    }

    inspection.table_status_page.return_value = page(
        [completed("one"), failed]
    )

    assert assess(readiness_setup)["full_load_status"] == "FAILED"


def test_independent_completed_subset_is_preserved(
    readiness_setup,
):
    inspection, _, _ = readiness_setup

    inspection.table_status_page.return_value = page(
        [
            completed("one"),
            {
                **completed("two"),
                "state": "Table error",
            },
        ]
    )

    result = assess(
        readiness_setup,
        selected_tables=[identity("one")],
    )

    assert result["full_load_ready"] is True
    assert len(result["tables"]) == 1


@pytest.mark.parametrize(
    "selection",
    [
        [],
        [identity("unknown")],
        [identity("one"), identity("one")],
    ],
)
def test_invalid_selection_is_rejected_before_inspection(
    readiness_setup,
    selection,
):
    inspection, _, _ = readiness_setup

    with pytest.raises(ValueError):
        assess(
            readiness_setup,
            selected_tables=selection,
        )

    inspection.task_status.assert_not_called()


def test_cdc_only_has_no_full_load_readiness(readiness_setup):
    inspection, binding, _ = readiness_setup
    binding["task_group"]["migration_type"] = "cdc"

    with pytest.raises(ValueError, match="migration type"):
        assess(readiness_setup)

    inspection.task_status.assert_not_called()


def test_page_budget_cannot_produce_partial_success(
    readiness_setup,
):
    inspection, _, _ = readiness_setup

    inspection.table_status_page.return_value = page(
        [completed("one"), completed("two")],
        "next",
    )

    with pytest.raises(
        InspectionError,
        match="budget exhausted",
    ):
        assess(readiness_setup, max_pages=1)


def test_marker_cycle_is_rejected(readiness_setup):
    inspection, _, _ = readiness_setup

    inspection.table_status_page.side_effect = [
        page([], "a"),
        page([], "b"),
        page([], "a"),
    ]

    with pytest.raises(InspectionError, match="marker cycle"):
        assess(readiness_setup)


def test_duplicate_selected_table_across_pages_is_rejected(
    readiness_setup,
):
    inspection, _, _ = readiness_setup

    inspection.table_status_page.side_effect = [
        page([completed("one")], "next"),
        page([completed("one")]),
    ]

    with pytest.raises(
        InspectionError,
        match="multiple statistics pages",
    ):
        assess(readiness_setup)


def test_unknown_table_state_blocks_instead_of_succeeding(
    readiness_setup,
):
    inspection, _, _ = readiness_setup

    inspection.table_status_page.return_value = page(
        [
            completed("one"),
            {
                **completed("two"),
                "state": "unknown",
            },
        ]
    )

    assert assess(readiness_setup)["full_load_status"] == "BLOCKED"


def test_cdc_failure_does_not_erase_finished_full_load(
    readiness_setup,
):
    inspection, _, _ = readiness_setup
    inspection.task_status.return_value["status"] = "failed"

    result = assess(readiness_setup)

    assert result["full_load_ready"] is True
    assert result["task_requires_attention"] is True


def test_failed_task_with_missing_load_is_not_ready(
    readiness_setup,
):
    inspection, _, _ = readiness_setup
    inspection.task_status.return_value["status"] = "failed"
    inspection.table_status_page.return_value = page([])

    assert assess(readiness_setup)["full_load_status"] == "FAILED"


def test_stopped_task_with_missing_load_is_blocked(
    readiness_setup,
):
    inspection, _, _ = readiness_setup
    inspection.task_status.return_value["status"] = "stopped"
    inspection.table_status_page.return_value = page([])

    assert assess(readiness_setup)["full_load_status"] == "BLOCKED"


def test_naive_extraction_boundary_is_rejected(readiness_setup):
    _, binding, readiness = readiness_setup

    with pytest.raises(ValueError, match="timezone-aware"):
        readiness.assess(
            binding,
            full_load_not_before=datetime(2026, 10, 26),
        )


def test_missing_finish_time_cannot_succeed(readiness_setup):
    inspection, _, _ = readiness_setup

    row = completed("one")
    row["full_load_finished_at"] = None

    inspection.table_status_page.return_value = page(
        [row, completed("two")]
    )

    assert assess(readiness_setup)["full_load_ready"] is False


def test_five_selected_tables_do_not_expand_to_ten(
    readiness_setup,
):
    inspection, binding, _ = readiness_setup

    binding["task_group"]["tables"] = [
        identity(f"table_{index}")
        for index in range(5)
    ]

    inspection.table_status_page.return_value = page(
        [
            completed(f"table_{index}")
            for index in range(10)
        ]
    )

    result = assess(readiness_setup)

    assert result["full_load_ready"] is True
    assert len(result["tables"]) == 5


def test_oracle_identity_is_preserved(readiness_setup):
    inspection, binding, _ = readiness_setup

    binding["task_group"]["tables"] = [
        {
            "schema_name": "DMS_USER",
            "table_name": "EXAMPLE",
        }
    ]

    inspection.table_status_page.return_value = page(
        [
            {
                **completed("EXAMPLE"),
                "schema_name": "DMS_USER",
            }
        ]
    )

    result = assess(readiness_setup)

    assert result["full_load_ready"] is True
    assert result["tables"][0]["schema_name"] == "DMS_USER"
    assert result["tables"][0]["table_name"] == "EXAMPLE"