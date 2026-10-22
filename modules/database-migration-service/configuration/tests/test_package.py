from importlib.metadata import version

import pytest
from jsonschema import Draft202012Validator

from ingestion_config.compiler import COMPILER_VERSION
from ingestion_config.schema import load_schema


@pytest.mark.parametrize(
    "name",
    [
        "service-v1.schema.json",
        "platform-v1.schema.json",
        "deployment-v1.schema.json",
    ],
)
def test_bundled_schema_is_available(name):
    schema = load_schema(name)

    Draft202012Validator.check_schema(schema)


def test_rejects_unknown_schema_name():
    with pytest.raises(
        ValueError,
        match="Unsupported bundled schema",
    ):
        load_schema("../another-file.json")


def test_installed_version_matches_compiler():
    assert version("moj-ingestion-config") == COMPILER_VERSION