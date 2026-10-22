import re
from typing import Any


RATE_PATTERN = re.compile(
    r"rate\(([1-9][0-9]*) "
    r"(minute|minutes|hour|hours|day|days)\)"
)


def validate_schedule(document: dict[str, Any]) -> None:
    schedule = document["schedule"]

    if not schedule["enabled"]:
        return

    if schedule["operation"] != "REINGESTION":
        raise ValueError(
            "schedule.operation: recurring schedules support "
            "REINGESTION only; initial ingestion uses "
            "an explicit request"
        )

    if schedule["timezone"] != "UTC":
        raise ValueError(
            "schedule.timezone: only UTC is supported "
            "for rate schedules"
        )

    match = RATE_PATTERN.fullmatch(schedule["expression"])

    if match is None:
        raise ValueError(
            "schedule.expression: expected "
            "rate(N minute[s]|hour[s]|day[s]) "
            "with a positive integer; cron and one-time "
            "schedules are unsupported"
        )

    amount = int(match.group(1))
    unit = match.group(2)

    if (
        amount == 1 and unit.endswith("s")
    ) or (
        amount != 1 and not unit.endswith("s")
    ):
        raise ValueError(
            "schedule.expression: use a singular unit for 1 "
            "and a plural unit for larger values"
        )

    unsupported = sorted(
        name
        for name, group in document["task_groups"].items()
        if group["mode"] != "full-load"
    )

    if unsupported:
        raise ValueError(
            "schedule: recurring re-ingestion supports "
            "full-load task groups only; ongoing CDC "
            "requires explicit lifecycle control. "
            "Unsupported task groups: "
            + ", ".join(unsupported)
        )