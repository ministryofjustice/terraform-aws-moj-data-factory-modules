import copy
from pathlib import Path

import pytest
import yaml

from ingestion_config.loader import ConfigurationError, load_service


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def scheduled_service():
    document = yaml.safe_load(
        (ROOT / "examples/postgres.yaml").read_text(
            encoding="utf-8"
        )
    )

    document["task_groups"]["integration"]["mode"] = "full-load"

    document["schedule"] = {
        "enabled": True,
        "expression": "rate(1 day)",
        "timezone": "UTC",
        "operation": "REINGESTION",
    }

    return document


def write_service(tmp_path, document):
    path = tmp_path / "service.yaml"

    path.write_text(
        yaml.safe_dump(document),
        encoding="utf-8",
    )

    return path


@pytest.mark.parametrize(
    "expression",
    [
        "rate(1 minute)",
        "rate(5 minutes)",
        "rate(1 hour)",
        "rate(2 hours)",
        "rate(1 day)",
        "rate(7 days)",
    ],
)
def test_accepts_supported_rates(
    tmp_path,
    scheduled_service,
    expression,
):
    scheduled_service["schedule"]["expression"] = expression

    actual = load_service(
        write_service(tmp_path, scheduled_service)
    )

    assert actual["schedule"]["expression"] == expression


@pytest.mark.parametrize(
    "expression",
    [
        "rate(0 days)",
        "rate(-1 days)",
        "rate(1.5 hours)",
        "rate(01 day)",
        "rate(1 days)",
        "rate(2 day)",
        "rate(1 week)",
        "cron(0 8 * * ? *)",
        "at(2026-12-01T08:00:00)",
    ],
)
def test_rejects_unsupported_expressions(
    tmp_path,
    scheduled_service,
    expression,
):
    scheduled_service["schedule"]["expression"] = expression

    with pytest.raises(
        ConfigurationError,
        match="schedule.expression",
    ):
        load_service(
            write_service(tmp_path, scheduled_service)
        )


@pytest.mark.parametrize(
    "timezone",
    ["Europe/London", "utc", " UTC"],
)
def test_rejects_unsupported_timezone(
    tmp_path,
    scheduled_service,
    timezone,
):
    scheduled_service["schedule"]["timezone"] = timezone

    with pytest.raises(
        ConfigurationError,
        match="schedule.timezone",
    ):
        load_service(
            write_service(tmp_path, scheduled_service)
        )


def test_rejects_recurring_initial_ingestion(
    tmp_path,
    scheduled_service,
):
    scheduled_service["schedule"]["operation"] = (
        "INITIAL_INGESTION"
    )

    with pytest.raises(
        ConfigurationError,
        match="schedule.operation",
    ):
        load_service(
            write_service(tmp_path, scheduled_service)
        )


def test_rejects_scheduled_cdc_replacement(
    tmp_path,
    scheduled_service,
):
    scheduled_service["task_groups"]["integration"]["mode"] = (
        "full-load-and-cdc"
    )

    with pytest.raises(
        ConfigurationError,
        match="full-load task groups only",
    ):
        load_service(
            write_service(tmp_path, scheduled_service)
        )


def test_rejects_mixed_scheduled_modes(
    tmp_path,
    scheduled_service,
):
    other = copy.deepcopy(
        scheduled_service["task_groups"]["integration"]
    )

    other["mode"] = "full-load-and-cdc"
    other["tables"][0]["table_name"] = "another_table"

    scheduled_service["task_groups"]["another"] = other

    with pytest.raises(
        ConfigurationError,
        match="Unsupported task groups: another",
    ):
        load_service(
            write_service(tmp_path, scheduled_service)
        )


def test_disabled_schedule_allows_cdc(
    tmp_path,
    scheduled_service,
):
    scheduled_service["schedule"] = {
        "enabled": False,
    }

    scheduled_service["task_groups"]["integration"]["mode"] = (
        "full-load-and-cdc"
    )

    actual = load_service(
        write_service(tmp_path, scheduled_service)
    )

    assert actual["schedule"] == {
        "enabled": False,
    }