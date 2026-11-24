import json
import re

from dms_runtime.history import RuntimeOperationHistory
from dms_runtime.raw_batch_actions import RawBatchAction


def _location(value, *, owner=False):
    fields = {"bucket_name", "prefix"}
    if owner:
        fields.add("expected_bucket_owner")

    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("Invalid Raw batch location fields")

    bucket = value["bucket_name"]
    prefix = value["prefix"]

    if not isinstance(bucket, str) or re.fullmatch(
        r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]",
        bucket,
    ) is None:
        raise ValueError("Invalid Raw batch bucket name")

    if (
        not isinstance(prefix, str)
        or not prefix.endswith("/")
        or any(character in prefix for character in "*?\\")
        or any(
            part in {"", ".", ".."}
            or part != part.strip()
            for part in prefix[:-1].split("/")
        )
    ):
        raise ValueError("Invalid Raw batch prefix")

    return value


def read_raw_batch_settings(environment, *, account_id):
    raw = environment.get("RAW_BATCH_HANDOFF_CONFIG")

    if raw is None:
        return None

    if (
        not isinstance(raw, str)
        or len(raw.encode("utf-8")) > 2048
    ):
        raise ValueError(
            "RAW_BATCH_HANDOFF_CONFIG must fit within 2048 bytes"
        )

    try:
        settings = json.loads(raw)
    except (ValueError, TypeError) as error:
        raise ValueError(
            "RAW_BATCH_HANDOFF_CONFIG must be valid JSON"
        ) from error

    if not isinstance(settings, dict) or set(settings) != {
        "manifest_location",
        "allowed_landings",
    }:
        raise ValueError("Invalid Raw batch handoff configuration")

    manifest = _location(
        settings["manifest_location"],
        owner=True,
    )

    if manifest["expected_bucket_owner"] != account_id:
        raise ValueError(
            "Raw batch manifest must belong to the runtime account"
        )

    landings = settings["allowed_landings"]

    if not isinstance(landings, list) or not landings:
        raise ValueError("At least one Raw landing is required")

    seen = set()

    for value in landings:
        landing = _location(value)
        identity = (
            landing["bucket_name"],
            landing["prefix"],
        )

        if identity in seen:
            raise ValueError("Duplicate Raw landing")

        seen.add(identity)

        if (
            landing["bucket_name"] == manifest["bucket_name"]
            and (
                landing["prefix"].startswith(manifest["prefix"])
                or manifest["prefix"].startswith(landing["prefix"])
            )
        ):
            raise ValueError(
                "Raw landing and manifest prefixes must not overlap"
            )

    return settings


def build_raw_batch_action(
    settings,
    *,
    client,
    lookup,
    readiness,
):
    if settings is None:
        return None

    return RawBatchAction(
        client=client,
        history=RuntimeOperationHistory(lookup),
        readiness=readiness,
        **settings,
    )