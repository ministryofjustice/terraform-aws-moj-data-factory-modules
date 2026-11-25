from copy import deepcopy
from dataclasses import asdict
from unittest.mock import Mock

import pytest

from dms_runtime.component_binding_action import (
    build_component_binding_entry_point,
)
from ingestion_registry.downstream_bindings import (
    DownstreamBindingConflict,
)
from ingestion_registry.ownership import OwnershipLost

from test_downstream_binding_lookup import component_setup
from test_downstream_binding_registry import setup
from test_ownership import scope_setup


@pytest.fixture
def action_setup(component_setup):
    state = component_setup
    claim = state["claim"]

    entry = build_component_binding_entry_point(
        registry=state["registry"],
        lookup=state["base"],
        clock=lambda: state["clock"]["now"],
    )

    event = {
        "request_version": 1,
        "action": "pin_downstream_component",
        "ingestion_id": claim.ingestion_id,
        "ingestion_owner": {
            "execution_id": claim.execution_id,
            "fencing_token": claim.fencing_token,
            "attempt": claim.attempt,
        },
        "component": "raw_history",
        "catalog_reference": asdict(state["reference"]),
    }

    return state, entry, event


def test_pin_action_replays_without_starting_work(action_setup):
    state, entry, event = action_setup

    first = entry(event, None)

    assert first["status"] == "PINNED"
    assert first["target_arn"].endswith(":history:3")
    assert entry(event, None) == first

    assert state["registry"]._read(
        state["pk"],
        "STAGE#HISTORY_BASELINE",
    ) is None


@pytest.mark.parametrize(
    "change",
    ["token", "expired"],
)
def test_pin_rejects_invalid_owner_before_catalog_read(
    action_setup,
    change,
    monkeypatch,
):
    state, entry, event = action_setup
    event = deepcopy(event)

    if change == "token":
        event["ingestion_owner"]["fencing_token"] += 1
    else:
        state["clock"]["now"] = (
            state["claim"].lease_expires_at + 1
        )

    read = Mock(
        wraps=state["base"].document_client.get_object
    )
    monkeypatch.setattr(
        state["base"].document_client,
        "get_object",
        read,
    )

    with pytest.raises(OwnershipLost):
        entry(event, None)

    read.assert_not_called()

    assert state["registry"]._read(
        state["pk"],
        "DOWNSTREAM_BINDING#raw_history",
    ) is None


@pytest.mark.parametrize(
    "change",
    [
        "extra",
        "component",
        "unversioned",
        "version",
        "size",
    ],
)
def test_pin_rejects_invalid_requests(action_setup, change):
    state, entry, event = action_setup
    event = deepcopy(event)

    if change == "extra":
        event["target_arn"] = "caller-supplied-target"

    elif change == "component":
        event["component"] = "unknown"

    elif change == "unversioned":
        del event["catalog_reference"]["version_id"]

    elif change == "version":
        event["request_version"] = True

    else:
        event["catalog_reference"]["object_key"] = "x" * 17000

    with pytest.raises(ValueError):
        entry(event, None)

    assert state["registry"]._read(
        state["pk"],
        "DOWNSTREAM_BINDING#raw_history",
    ) is None


def test_pin_checks_service_scope_before_writing(action_setup):
    state, entry, event = action_setup

    state["base"].resolve.side_effect = ValueError(
        "outside service scope"
    )

    with pytest.raises(
        ValueError,
        match="outside service scope",
    ):
        entry(event, None)

    assert state["registry"]._read(
        state["pk"],
        "DOWNSTREAM_BINDING#raw_history",
    ) is None


def test_pin_rejects_catalog_outside_boundary(action_setup):
    state, entry, event = action_setup
    event = deepcopy(event)

    event["catalog_reference"]["object_key"] = (
        "outside/components.json"
    )

    with pytest.raises(
        DownstreamBindingConflict,
        match="published document boundary",
    ):
        entry(event, None)

    assert state["registry"]._read(
        state["pk"],
        "DOWNSTREAM_BINDING#raw_history",
    ) is None