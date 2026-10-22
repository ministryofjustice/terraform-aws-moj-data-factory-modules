import copy
import json
from pathlib import Path

import pytest
import yaml

from ingestion_config.compiler import (
    canonical_json,
    compile_service,
    fingerprint,
)


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


@pytest.fixture
def configuration(tmp_path):
    service = yaml.safe_load(
        (EXAMPLES / "postgres.yaml").read_text(
            encoding="utf-8"
        )
    )

    platform = yaml.safe_load(
        (EXAMPLES / "platform-development.yaml").read_text(
            encoding="utf-8"
        )
    )

    def compile_config(generation="generation-one"):
        service_path = tmp_path / "service.yaml"
        platform_path = tmp_path / "platform.yaml"

        service_path.write_text(
            yaml.safe_dump(service),
            encoding="utf-8",
        )

        platform_path.write_text(
            yaml.safe_dump(platform),
            encoding="utf-8",
        )

        return compile_service(
            service_path,
            platform_path,
            extraction_generation=generation,
        )

    return service, platform, compile_config


@pytest.mark.parametrize("engine", ["postgres", "oracle"])
def test_compiles_engine_examples(engine):
    result = compile_service(
        EXAMPLES / f"{engine}.yaml",
        EXAMPLES / "platform-development.yaml",
        extraction_generation="generation-one",
    )

    assert result["runtime_configuration"]["source_endpoint"][
        "engine"
    ] == engine

    assert result["module_inputs"]

    json.dumps(result, allow_nan=False)


def test_repeated_compilation_is_identical(configuration):
    _, _, compile_config = configuration

    assert canonical_json(compile_config()) == (
        canonical_json(compile_config())
    )


def test_generation_changes_boundary_not_configuration(
    configuration,
):
    _, _, compile_config = configuration

    first = compile_config()
    replacement = compile_config("generation-two")

    assert first["configuration_revision"] == (
        replacement["configuration_revision"]
    )

    assert first["artifact_revision"] != (
        replacement["artifact_revision"]
    )

    first_group = first["module_inputs"]["integration"]
    next_group = replacement["module_inputs"]["integration"]

    assert first_group["name"] != next_group["name"]

    assert first_group["target"]["bucket_folder"] != (
        next_group["target"]["bucket_folder"]
    )


def test_reordered_selections_produce_identical_output(
    configuration,
):
    service, platform, compile_config = configuration

    tables = service["task_groups"]["integration"]["tables"]

    other = copy.deepcopy(tables[0])
    other["table_name"] = "another_table"
    tables.append(other)

    first = compile_config()

    tables.reverse()

    binding = platform["services"][service["service"]]
    network = platform["network_profiles"][
        binding["network_profile"]
    ]
    network["subnet_ids"].reverse()

    assert compile_config() == first


def test_setting_change_updates_revision_without_renaming(
    configuration,
):
    service, platform, compile_config = configuration

    first = compile_config()

    binding = platform["services"][service["service"]]
    profile = platform["replication_profiles"][
        binding["replication_profile"]
    ]

    profile["allocated_storage"] += 100

    second = compile_config()

    assert first["configuration_revision"] != (
        second["configuration_revision"]
    )

    assert first["module_inputs"]["integration"]["name"] == (
        second["module_inputs"]["integration"]["name"]
    )


def test_compilation_selects_only_five_of_ten_tables(
    configuration,
):
    service, _, compile_config = configuration

    group = service["task_groups"]["integration"]
    template = group["tables"][0]

    available = []

    for number in range(1, 11):
        table = copy.deepcopy(template)
        table["table_name"] = f"table_{number}"
        available.append(table)

    group["tables"] = available[::2]

    result = compile_config()

    task = result["module_inputs"]["integration"][
        "replication_tasks"
    ]["integration"]

    rules = json.loads(task["table_mappings"])["rules"]

    assert len(rules) == 5

    assert {
        rule["object-locator"]["table-name"]
        for rule in rules
    } == {
        "table_1",
        "table_3",
        "table_5",
        "table_7",
        "table_9",
    }

    assert all(
        rule["rule-action"] == "explicit"
        for rule in rules
    )


def test_runtime_preserves_contract_intentions(configuration):
    service, _, compile_config = configuration

    generated = compile_config()

    generated_table = generated["runtime_configuration"][
        "task_groups"
    ]["integration"]["tables"][0]

    assert generated_table["contract"]["strategy"] == (
        "generate_if_missing"
    )

    service["task_groups"]["integration"]["tables"][0][
        "contract"
    ] = {
        "strategy": "existing",
        "contract_version": "v12",
    }

    existing = compile_config()

    existing_table = existing["runtime_configuration"][
        "task_groups"
    ]["integration"]["tables"][0]

    assert existing_table["contract"] == {
        "strategy": "existing",
        "contract_version": "v12",
    }

    assert "contract_id" not in existing_table
    assert "ingestion_version" not in existing_table


def test_task_groups_have_separate_resources(configuration):
    service, _, compile_config = configuration

    groups = service["task_groups"]

    other = copy.deepcopy(groups["integration"])
    other["tables"][0]["table_name"] = "another_table"
    groups["another"] = other

    output = compile_config()["module_inputs"]

    assert set(output) == {"integration", "another"}

    assert output["integration"]["name"] != (
        output["another"]["name"]
    )

    assert output["integration"]["target"]["bucket_folder"] != (
        output["another"]["target"]["bucket_folder"]
    )


def test_artifact_revision_covers_complete_output(configuration):
    _, _, compile_config = configuration

    result = compile_config()
    revision = result.pop("artifact_revision")

    assert revision == fingerprint(result)