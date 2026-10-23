import copy

import pytest

from ingestion_registry.extraction_policy import (
    ExtractionPolicy,
    resolve_extraction_policy,
)
from ingestion_registry.resource_bindings import BindingConflict


@pytest.mark.parametrize(
    ("mode", "includes_full_load", "includes_cdc"),
    [
        ("full-load", True, False),
        ("full-load-and-cdc", True, True),
        ("cdc", False, True),
    ],
)
def test_policy_describes_the_configured_mode(
    mode,
    includes_full_load,
    includes_cdc,
):
    policy = ExtractionPolicy(migration_type=mode)

    assert policy.includes_full_load is includes_full_load
    assert policy.includes_cdc is includes_cdc


@pytest.mark.parametrize(
    "mode",
    [None, "", "FULL", "full_load", True, [], {}],
)
def test_unsupported_modes_are_rejected(mode):
    with pytest.raises(ValueError, match="migration_type"):
        ExtractionPolicy(migration_type=mode)


def test_policy_uses_the_resolved_task_group():
    policy = resolve_extraction_policy(
        {
            "task_group": {
                "migration_type": "full-load-and-cdc",
            }
        }
    )

    assert policy.migration_type == "full-load-and-cdc"
    assert policy.includes_cdc is True


@pytest.mark.parametrize(
    "binding",
    [
        None,
        [],
        {},
        {"task_group": None},
        {"task_group": []},
        {"task_group": {}},
    ],
)
def test_incomplete_bindings_have_no_default_mode(binding):
    with pytest.raises(ValueError):
        resolve_extraction_policy(binding)


def test_binding_pin_rejects_a_missing_mode(binding_setup):
    _, _, _, _, claim, bindings, binding = binding_setup
    invalid = copy.deepcopy(binding)
    invalid["task_group"].pop("migration_type")

    with pytest.raises(BindingConflict, match="migration_type"):
        bindings.pin(claim, invalid)


def test_binding_pin_rejects_an_unsupported_mode(binding_setup):
    _, _, _, _, claim, bindings, binding = binding_setup
    invalid = copy.deepcopy(binding)
    invalid["task_group"]["migration_type"] = "unknown"

    with pytest.raises(BindingConflict, match="migration_type"):
        bindings.pin(claim, invalid)


def test_pinned_mode_cannot_be_changed(binding_setup):
    _, _, _, _, claim, bindings, binding = binding_setup
    bindings.pin(claim, binding)

    changed = copy.deepcopy(binding)
    changed["task_group"]["migration_type"] = "full-load"

    with pytest.raises(BindingConflict, match="different pinned binding"):
        bindings.pin(claim, changed)