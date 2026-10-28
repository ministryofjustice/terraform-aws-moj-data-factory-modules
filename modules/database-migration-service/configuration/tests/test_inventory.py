import json
import shutil
from pathlib import Path

import pytest

from ingestion_config.cli import main
from ingestion_config.inventory import compile_inventory
from ingestion_config.loader import ConfigurationError


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture
def inventory(tmp_path):
    services = tmp_path / "services"
    services.mkdir()

    for engine in ("postgres", "oracle"):
        shutil.copyfile(
            EXAMPLES / f"{engine}.yaml",
            services / f"sandbox-{engine}.yaml",
        )

    return services, {
        "sandbox-postgres": "sandbox-v1",
        "sandbox-oracle": "sandbox-v1",
    }


def test_two_services_compile_without_terraform_blocks(inventory):
    directory, generations = inventory

    artifacts = compile_inventory(
        directory,
        EXAMPLES / "platform-development.yaml",
        generations,
    )["compiled_artifacts"]

    assert set(artifacts) == {
        f"{name}/{generation}"
        for name, generation in generations.items()
    }

    assert (
        artifacts["sandbox-postgres/sandbox-v1"]["runtime_configuration"]
        ["source_endpoint"]["engine"]
        == "postgres"
    )

    assert (
        artifacts["sandbox-oracle/sandbox-v1"]["runtime_configuration"]
        ["source_endpoint"]["engine"]
        == "oracle"
    )


def test_missing_generation_is_rejected(inventory):
    directory, _ = inventory

    with pytest.raises(ConfigurationError, match="Generation keys"):
        compile_inventory(
            directory,
            EXAMPLES / "platform-development.yaml",
            {},
        )


def test_filename_identity_mismatch_is_rejected(inventory):
    directory, generations = inventory

    (directory / "sandbox-postgres.yaml").rename(directory / "other.yaml")
    generations["other"] = generations.pop("sandbox-postgres")

    with pytest.raises(ConfigurationError, match="filename must match"):
        compile_inventory(
            directory,
            EXAMPLES / "platform-development.yaml",
            generations,
        )


def test_duplicate_extensions_are_rejected(inventory):
    directory, generations = inventory

    shutil.copyfile(
        directory / "sandbox-postgres.yaml",
        directory / "sandbox-postgres.yml",
    )

    with pytest.raises(ConfigurationError, match="Duplicate"):
        compile_inventory(
            directory,
            EXAMPLES / "platform-development.yaml",
            generations,
        )


def test_cli_creates_tfvars_and_does_not_overwrite(inventory, tmp_path):
    directory, generations = inventory

    generation_file = tmp_path / "generations.json"
    generation_file.write_text(json.dumps(generations))

    output = tmp_path / "compiled.auto.tfvars.json"

    arguments = [
        "compile-directory",
        "--services",
        str(directory),
        "--platform",
        str(EXAMPLES / "platform-development.yaml"),
        "--generations",
        str(generation_file),
        "--output",
        str(output),
    ]

    assert main(arguments) == 0

    original = output.read_bytes()

    assert set(json.loads(original)["compiled_artifacts"]) == {
        f"{name}/{generation}"
        for name, generation in generations.items()
    }
    assert main(arguments) == 2
    assert output.read_bytes() == original

def test_new_generation_retains_old_artifacts_unchanged(inventory):
    import copy
    import yaml

    directory, generations = inventory
    platform = EXAMPLES / "platform-development.yaml"

    original = compile_inventory(directory, platform, generations)
    snapshot = copy.deepcopy(original)

    path = directory / "sandbox-postgres.yaml"
    service = yaml.safe_load(path.read_text())

    service["task_groups"]["integration"]["tables"][0][
        "table_name"
    ] = "another_table"

    path.write_text(yaml.safe_dump(service))

    newer = compile_inventory(
        directory,
        platform,
        {
            **generations,
            "sandbox-postgres": "sandbox-v2",
        },
        retained=original,
    )

    assert original == snapshot

    for key, artifact in snapshot["compiled_artifacts"].items():
        assert newer["compiled_artifacts"][key] == artifact

    assert set(newer["compiled_artifacts"]) == {
        "sandbox-postgres/sandbox-v1",
        "sandbox-postgres/sandbox-v2",
        "sandbox-oracle/sandbox-v1",
    }

    old = newer["compiled_artifacts"]["sandbox-postgres/sandbox-v1"]
    new = newer["compiled_artifacts"]["sandbox-postgres/sandbox-v2"]

    assert (
        old["module_inputs"]["integration"]["name"]
        != new["module_inputs"]["integration"]["name"]
    )

    assert (
        old["module_inputs"]["integration"]["target"]["bucket_folder"]
        != new["module_inputs"]["integration"]["target"]["bucket_folder"]
    )


def test_rejects_rewriting_prepared_generation(inventory):
    import yaml

    directory, generations = inventory
    platform = EXAMPLES / "platform-development.yaml"

    original = compile_inventory(directory, platform, generations)

    path = directory / "sandbox-postgres.yaml"
    service = yaml.safe_load(path.read_text())

    service["task_groups"]["integration"]["tables"][0][
        "table_name"
    ] = "another_table"

    path.write_text(yaml.safe_dump(service))

    with pytest.raises(
        ConfigurationError,
        match="fresh extraction generation",
    ):
        compile_inventory(
            directory,
            platform,
            generations,
            retained=original,
        )


def test_same_prepared_generation_is_idempotent(inventory):
    directory, generations = inventory
    platform = EXAMPLES / "platform-development.yaml"

    original = compile_inventory(directory, platform, generations)

    assert compile_inventory(
        directory,
        platform,
        generations,
        retained=original,
    ) == original


def test_rejects_modified_retained_artifact(inventory):
    directory, generations = inventory
    platform = EXAMPLES / "platform-development.yaml"

    original = compile_inventory(directory, platform, generations)

    original["compiled_artifacts"]["sandbox-postgres/sandbox-v1"][
        "configuration_revision"
    ] = "0" * 64

    with pytest.raises(ConfigurationError, match="revision"):
        compile_inventory(
            directory,
            platform,
            generations,
            retained=original,
        )


def test_rejects_misnamed_retained_generation(inventory):
    directory, generations = inventory
    platform = EXAMPLES / "platform-development.yaml"

    original = compile_inventory(directory, platform, generations)

    original["compiled_artifacts"]["wrong-key"] = (
        original["compiled_artifacts"].pop(
            "sandbox-postgres/sandbox-v1"
        )
    )

    with pytest.raises(ConfigurationError, match="Deployment key"):
        compile_inventory(
            directory,
            platform,
            generations,
            retained=original,
        )


def test_cli_retains_previous_deployment_inventory(inventory, tmp_path):
    directory, generations = inventory
    platform = EXAMPLES / "platform-development.yaml"

    old = compile_inventory(directory, platform, generations)

    retained_path = tmp_path / "retained.json"
    retained_path.write_text(json.dumps(old))

    generation_path = tmp_path / "generations.json"
    generation_path.write_text(
        json.dumps({
            **generations,
            "sandbox-postgres": "sandbox-v2",
        })
    )

    output = tmp_path / "next.json"

    assert main([
        "compile-directory",
        "--services",
        str(directory),
        "--platform",
        str(platform),
        "--generations",
        str(generation_path),
        "--retained",
        str(retained_path),
        "--output",
        str(output),
    ]) == 0

    assert len(
        json.loads(output.read_text())["compiled_artifacts"]
    ) == 3

    assert json.loads(retained_path.read_text()) == old