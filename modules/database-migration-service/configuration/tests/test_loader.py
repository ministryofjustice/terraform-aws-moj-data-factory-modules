import copy
import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from ingestion_config.loader import ConfigurationError, load_service
from ingestion_config.schema import load_schema


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def service():
    return yaml.safe_load(
        (ROOT / "examples/postgres.yaml").read_text(encoding="utf-8")
    )


def write_service(tmp_path, document):
    path = tmp_path / "service.yaml"
    path.write_text(
        yaml.safe_dump(document),
        encoding="utf-8",
    )
    return path


def table(document):
    return document["task_groups"]["integration"]["tables"][0]


def test_schema_is_valid():
    schema = load_schema("service-v1.schema.json")

    Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize("engine", ["postgres", "oracle"])
def test_examples(engine):
    document = load_service(ROOT / f"examples/{engine}.yaml")

    assert document["source"]["engine"] == engine


def test_preserves_identifier_case(tmp_path, service):
    service["source"]["database_name"] = "MixedCaseDB"
    table(service)["schema_name"] = "MixedSchema"
    table(service)["table_name"] = "MixedTable"

    actual = load_service(write_service(tmp_path, service))

    assert actual["source"]["database_name"] == "MixedCaseDB"
    assert table(actual)["schema_name"] == "MixedSchema"
    assert table(actual)["table_name"] == "MixedTable"


@pytest.mark.parametrize(
    "field",
    ["password", "ingestion_version", "execution_id", "attempt"],
)
def test_rejects_unknown_root_fields(tmp_path, service, field):
    service[field] = "unexpected"

    with pytest.raises(
        ConfigurationError,
        match="Additional properties",
    ):
        load_service(write_service(tmp_path, service))


def test_rejects_missing_source(tmp_path, service):
    del service["source"]

    with pytest.raises(
        ConfigurationError,
        match="required property",
    ):
        load_service(write_service(tmp_path, service))


@pytest.mark.parametrize("version", [0, 2, "1", True])
def test_rejects_unsupported_version(tmp_path, service, version):
    service["configuration_version"] = version

    with pytest.raises(ConfigurationError):
        load_service(write_service(tmp_path, service))


@pytest.mark.parametrize(
    "name",
    ["%", "*", "table?", " table", "table "],
)
def test_rejects_unsupported_table_selection(
    tmp_path,
    service,
    name,
):
    table(service)["table_name"] = name

    with pytest.raises(
        ConfigurationError,
        match="table_name",
    ):
        load_service(write_service(tmp_path, service))


@pytest.mark.parametrize("another_group", [False, True])
def test_rejects_duplicate_table(
    tmp_path,
    service,
    another_group,
):
    if another_group:
        service["task_groups"]["second"] = copy.deepcopy(
            service["task_groups"]["integration"]
        )
    else:
        service["task_groups"]["integration"]["tables"].append(
            copy.deepcopy(table(service))
        )

    with pytest.raises(
        ConfigurationError,
        match="duplicate table",
    ):
        load_service(write_service(tmp_path, service))


def test_accepts_same_table_name_in_different_schemas(
    tmp_path,
    service,
):
    other = copy.deepcopy(table(service))
    other["schema_name"] = "other_schema"

    service["task_groups"]["integration"]["tables"].append(other)

    actual = load_service(write_service(tmp_path, service))

    assert len(
        actual["task_groups"]["integration"]["tables"]
    ) == 2


def test_accepts_existing_contract(tmp_path, service):
    table(service)["contract"] = {
        "strategy": "existing",
        "contract_version": "v12",
    }

    actual = load_service(write_service(tmp_path, service))

    assert table(actual)["contract"]["contract_version"] == "v12"


@pytest.mark.parametrize(
    "version",
    ["v0", "v01", "V1", "latest"],
)
def test_rejects_invalid_contract_version(
    tmp_path,
    service,
    version,
):
    table(service)["contract"] = {
        "strategy": "existing",
        "contract_version": version,
    }

    with pytest.raises(
        ConfigurationError,
        match="contract",
    ):
        load_service(write_service(tmp_path, service))


def test_rejects_conflicting_table_mode(tmp_path, service):
    table(service)["mode"] = "full-load"

    with pytest.raises(
        ConfigurationError,
        match="Additional properties",
    ):
        load_service(write_service(tmp_path, service))


@pytest.mark.parametrize(
    "text, message",
    [
        (
            "service: first\nservice: second\n",
            "Duplicate key",
        ),
        (
            "source:\n  engine: postgres\n  engine: oracle\n",
            "Duplicate key",
        ),
        (
            "service: &name example\nother: *name\n",
            "anchors and aliases",
        ),
        (
            "1: value\n",
            "Keys must be strings",
        ),
        (
            "source: [\n",
            ".*",
        ),
        (
            "---\nservice: first\n---\nservice: second\n",
            ".*",
        ),
        (
            "",
            ".*",
        ),
    ],
)
def test_rejects_invalid_yaml(tmp_path, text, message):
    path = tmp_path / "service.yaml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(
        ConfigurationError,
        match=message,
    ):
        load_service(path)