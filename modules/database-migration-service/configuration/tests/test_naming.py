import re

import pytest

from ingestion_config.loader import ConfigurationError
from ingestion_config.naming import build_resource_names


@pytest.fixture
def identity():
    return {
        "environment": "development",
        "region": "eu-west-2",
        "service": "sandbox-postgres",
        "database_name": "dms_test",
        "task_group": "integration",
        "extraction_generation": "generation-one",
    }


def test_same_identity_produces_same_names(identity):
    assert build_resource_names(**identity) == (
        build_resource_names(**identity)
    )


@pytest.mark.parametrize(
    "field, replacement",
    [
        ("environment", "test"),
        ("region", "eu-west-1"),
        ("service", "another-service"),
        ("database_name", "another_database"),
        ("task_group", "another-group"),
        ("extraction_generation", "generation-two"),
    ],
)
def test_different_identity_produces_different_names(
    identity,
    field,
    replacement,
):
    original = build_resource_names(**identity)

    changed = {
        **identity,
        field: replacement,
    }

    assert build_resource_names(**changed) != original


def test_database_case_is_part_of_identity(identity):
    changed = {
        **identity,
        "database_name": "DMS_TEST",
    }

    assert build_resource_names(**identity) != (
        build_resource_names(**changed)
    )


def test_long_labels_fit_actual_wrapper_instance_id(identity):
    identity["service"] = "a" * 63
    identity["task_group"] = "b" * 63

    names = build_resource_names(**identity)

    instance_id = (
        f"{names['name']}-dms-instance-"
        f"{identity['environment']}"
    )

    assert len(instance_id) <= 63
    assert len(names["replication_task_id"]) <= 255


def test_truncation_does_not_hide_different_service(identity):
    first = {
        **identity,
        "service": "shared-prefix-one",
    }

    second = {
        **identity,
        "service": "shared-prefix-two",
    }

    assert build_resource_names(**first) != (
        build_resource_names(**second)
    )


def test_truncated_hyphen_does_not_create_double_hyphen(
    identity,
):
    identity["service"] = "abcdefghi-example"
    identity["task_group"] = "abcde-example"

    for value in build_resource_names(**identity).values():
        assert "--" not in value

        assert re.fullmatch(
            r"[a-z][a-z0-9-]*[a-z0-9]",
            value,
        )


@pytest.mark.parametrize(
    "generation",
    [
        "",
        "1",
        "generation_one",
        "generation--one",
        "Generation-one",
    ],
)
def test_rejects_invalid_deployment_generation(
    identity,
    generation,
):
    identity["extraction_generation"] = generation

    with pytest.raises(
        ConfigurationError,
        match="extraction_generation",
    ):
        build_resource_names(**identity)


def test_rejects_environment_that_breaks_wrapper_limit(
    identity,
):
    identity["environment"] = "a" * 63

    with pytest.raises(
        ConfigurationError,
        match="exceeds 63 characters",
    ):
        build_resource_names(**identity)