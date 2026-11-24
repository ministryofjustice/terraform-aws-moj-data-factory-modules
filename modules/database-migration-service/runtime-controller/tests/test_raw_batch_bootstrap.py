import copy
import json
from unittest.mock import Mock

import pytest

from dms_runtime.lambda_function import build_handler
from dms_runtime.raw_batch_actions import RawBatchAction
from dms_runtime.raw_batch_bootstrap import read_raw_batch_settings

from test_lambda_function import deployment_settings


@pytest.fixture
def settings():
    return {
        "manifest_location": {
            "bucket_name": "example-evidence",
            "prefix": "batches/",
            "expected_bucket_owner": "111122223333",
        },
        "allowed_landings": [
            {
                "bucket_name": "example-raw",
                "prefix": "landing/v1/",
            },
        ],
    }


def parse(settings):
    return read_raw_batch_settings(
        {"RAW_BATCH_HANDOFF_CONFIG": json.dumps(settings)},
        account_id="111122223333",
    )


def test_missing_config_keeps_action_disabled(deployment_settings):
    handler = build_handler(
        deployment_settings,
        session=Mock(),
    )

    assert handler.owned_action_handler.raw_batch_action is None


def test_config_connects_shared_dependencies(
    settings,
    deployment_settings,
):
    deployment_settings["RAW_BATCH_HANDOFF_CONFIG"] = json.dumps(settings)

    handler = build_handler(
        deployment_settings,
        session=Mock(),
    )
    action = handler.owned_action_handler.raw_batch_action

    assert isinstance(action, RawBatchAction)
    assert action.history.lookup is handler.lookup
    assert action.readiness is handler.full_load
    assert action.allowed_landings == tuple(settings["allowed_landings"])


@pytest.mark.parametrize(
    "prefix",
    [
        "/raw/",
        "raw",
        "raw//",
        "raw/../",
        "raw/*/",
        "raw/?/",
        "raw/\\/",
    ],
)
def test_rejects_invalid_landing_prefix(settings, prefix):
    settings["allowed_landings"][0]["prefix"] = prefix

    with pytest.raises(ValueError):
        parse(settings)


def test_rejects_overlapping_destinations(settings):
    settings["manifest_location"].update(
        bucket_name="example-raw",
        prefix="landing/",
    )

    with pytest.raises(ValueError, match="overlap"):
        parse(settings)


def test_rejects_wrong_owner(settings):
    settings["manifest_location"]["expected_bucket_owner"] = "999988887777"

    with pytest.raises(ValueError, match="account"):
        parse(settings)


def test_rejects_duplicate_locations(settings):
    settings["allowed_landings"].append(
        copy.deepcopy(settings["allowed_landings"][0])
    )

    with pytest.raises(ValueError, match="Duplicate"):
        parse(settings)


def test_rejects_empty_locations(settings):
    settings["allowed_landings"] = []

    with pytest.raises(ValueError):
        parse(settings)


def test_rejects_unknown_fields(settings):
    settings["allow_all"] = True

    with pytest.raises(ValueError):
        parse(settings)


def test_invalid_config_rejected_before_clients(deployment_settings):
    deployment_settings["RAW_BATCH_HANDOFF_CONFIG"] = "{bad"
    session = Mock()

    with pytest.raises(ValueError):
        build_handler(
            deployment_settings,
            session=session,
        )

    session.client.assert_not_called()