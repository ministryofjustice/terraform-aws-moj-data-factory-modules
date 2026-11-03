import base64
import copy
import json
from pathlib import Path

import pytest

from ingestion_config.cli import main
from ingestion_config.compiler import compile_service
from ingestion_config.deployment_inputs import build_deployment_inputs
from ingestion_config.loader import ConfigurationError


ACCOUNT = "111122223333"
COMMIT = "296c2060d38b1dab02d2560f4c058201ac2b757f"

KEY = (
    f"arn:aws:kms:eu-west-2:{ACCOUNT}:"
    "key/11111111-1111-1111-1111-111111111111"
)


@pytest.fixture
def inputs():
    examples = Path(__file__).resolve().parents[1] / "examples"

    artifacts = {}

    for engine in ("postgres", "oracle"):
        artifact = compile_service(
            examples / f"{engine}.yaml",
            examples / "platform-development.yaml",
            extraction_generation="sandbox-v1",
        )

        source = artifact["runtime_configuration"]["source"]

        artifacts[f"{source}/sandbox-v1"] = artifact

    digest = "ab" * 32

    return {
        "inventory": {
            "compiled_artifacts": artifacts,
        },
        "foundations": {
            "environment": "development",
            "account_id": ACCOUNT,
            "region": "eu-west-2",
            "registry": {
                "table_name": "example-registry",
                "table_arn": (
                    f"arn:aws:dynamodb:eu-west-2:{ACCOUNT}:"
                    "table/example-registry"
                ),
            },
            "published_documents": {
                "bucket_name": "example-configuration",
                "bucket_owner": ACCOUNT,
                "kms_key_arns": [KEY],
            },
            "source_credentials": {
                "sandbox-postgres": {
                    "mode": "synchronise_then_test",
                    "sync_function_arn": (
                        f"arn:aws:lambda:eu-west-2:{ACCOUNT}:"
                        "function:example-sync"
                    ),
                },
                "sandbox-oracle": {
                    "mode": "connection_test",
                },
            },
            "replication_capacities": {
                "sandbox-shared": {
                    "vpc_id": "vpc-0123456789abcdef0",
                    "replication_instance_arn": (
                        f"arn:aws:dms:eu-west-2:{ACCOUNT}:"
                        "rep:SHAREDPOOL"
                    ),
                    "replication_instance_id": "example-shared",
                    "replication_security_group_id": (
                        "sg-0123456789abcdef0"
                    ),
                    "replication_security_group_ids": [
                        "sg-0123456789abcdef0",
                    ],
                    "replication_subnet_group_id": (
                        "example-shared-subnets"
                    ),
                    "task_log_group": {
                        "name": "dms-tasks-example-shared",
                        "arn": (
                            f"arn:aws:logs:eu-west-2:{ACCOUNT}:"
                            "log-group:dms-tasks-example-shared"
                        ),
                    },
                },
            },
        },
        "runtime_package": {
            "source_repository": (
                "ministryofjustice/"
                "terraform-aws-moj-data-factory-modules"
            ),
            "source_commit": COMMIT,
            "region": "eu-west-2",
            "python_version": "3.12",
            "architecture": "x86_64",
            "handler": "dms_runtime.lambda_function.lambda_handler",
            "sha256": digest,
            "source_code_hash": base64.b64encode(
                bytes.fromhex(digest)
            ).decode("ascii"),
            "bucket_name": "example-artifacts",
            "kms_key_arn": KEY,
            "object_key": (
                "lambda/dms-runtime-controller/"
                f"{COMMIT}/{digest}/package.zip"
            ),
            "version_id": "package-version-one",
        },
        "capacity_assignments": {
            key: {"integration": "sandbox-shared"}
            for key in artifacts
        },
        "retained_inventory": {
            "compiled_artifacts": {},
        },
        "retained_assignments": {},
        "compiler_commit": COMMIT,
    }


def test_two_services_use_one_composition_without_mutating_inputs(inputs):
    original = copy.deepcopy(inputs)

    result = build_deployment_inputs(**inputs)["deployment"]

    assert set(result["compiled_artifacts"]) == {
        "sandbox-postgres/sandbox-v1",
        "sandbox-oracle/sandbox-v1",
    }

    assert (
        result["runtime_settings"]["sandbox-postgres"]
        ["published_documents"]["prefix"]
        == "services/sandbox-postgres/"
    )

    assert (
        result["runtime_settings"]["sandbox-oracle"]
        ["source_credentials"]
        == {"mode": "connection_test"}
    )

    assert result["artifact"]["version_id"] == "package-version-one"
    assert inputs == original


@pytest.mark.parametrize(
    "field,value",
    [
        ("environment", "production"),
        ("region", "eu-west-1"),
    ],
)
def test_rejects_other_environments(inputs, field, value):
    inputs["foundations"][field] = value

    with pytest.raises(ConfigurationError, match="restricted"):
        build_deployment_inputs(**inputs)


def test_rejects_modified_compiler_artifact(inputs):
    inputs["inventory"]["compiled_artifacts"][
        "sandbox-postgres/sandbox-v1"
    ]["configuration_revision"] = "0" * 64

    with pytest.raises(ConfigurationError, match="revision"):
        build_deployment_inputs(**inputs)


def test_rejects_checksum_disagreement(inputs):
    inputs["runtime_package"]["source_code_hash"] = "wrong"

    with pytest.raises(
        ConfigurationError,
        match="checksums disagree",
    ):
        build_deployment_inputs(**inputs)


def test_rejects_unversioned_runtime_package(inputs):
    inputs["runtime_package"]["version_id"] = "null"

    with pytest.raises(ConfigurationError, match="versioned"):
        build_deployment_inputs(**inputs)


def test_rejects_runtime_key_in_another_account(inputs):
    inputs["runtime_package"]["kms_key_arn"] = KEY.replace(
        ACCOUNT,
        "999900001111",
    )

    with pytest.raises(
        ConfigurationError,
        match="deployment object",
    ):
        build_deployment_inputs(**inputs)


def test_rejects_missing_capacity_assignment(inputs):
    inputs["capacity_assignments"][
        "sandbox-postgres/sandbox-v1"
    ] = {}

    with pytest.raises(
        ConfigurationError,
        match="Every task group",
    ):
        build_deployment_inputs(**inputs)


def test_rejects_unapproved_pool(inputs):
    inputs["capacity_assignments"][
        "sandbox-postgres/sandbox-v1"
    ]["integration"] = "other"

    with pytest.raises(
        ConfigurationError,
        match="pool does not exist",
    ):
        build_deployment_inputs(**inputs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("vpc_id", "vpc-other"),
        ("replication_security_group_ids", []),
        ("task_log_group", None),
    ],
)
def test_rejects_capacity_without_source_access_or_logs(
    inputs,
    field,
    value,
):
    inputs["foundations"]["replication_capacities"][
        "sandbox-shared"
    ][field] = value

    with pytest.raises(
        ConfigurationError,
        match="required VPC, source access and logs",
    ):
        build_deployment_inputs(**inputs)


def test_rejects_removing_retained_generation(inputs):
    inputs["retained_inventory"] = copy.deepcopy(
        inputs["inventory"]
    )

    inputs["retained_assignments"] = copy.deepcopy(
        inputs["capacity_assignments"]
    )

    key = "sandbox-postgres/sandbox-v1"

    del inputs["inventory"]["compiled_artifacts"][key]
    del inputs["capacity_assignments"][key]

    with pytest.raises(
        ConfigurationError,
        match="removed or changed",
    ):
        build_deployment_inputs(**inputs)


def test_rejects_reassigning_retained_generation(inputs):
    inputs["retained_inventory"] = copy.deepcopy(
        inputs["inventory"]
    )

    inputs["retained_assignments"] = copy.deepcopy(
        inputs["capacity_assignments"]
    )

    inputs["capacity_assignments"][
        "sandbox-postgres/sandbox-v1"
    ]["integration"] = "other"

    with pytest.raises(
        ConfigurationError,
        match="capacity assignment was changed",
    ):
        build_deployment_inputs(**inputs)


def test_accepts_unchanged_retained_generations(inputs):
    inputs["retained_inventory"] = copy.deepcopy(
        inputs["inventory"]
    )

    inputs["retained_assignments"] = copy.deepcopy(
        inputs["capacity_assignments"]
    )

    result = build_deployment_inputs(**inputs)["deployment"]

    assert (
        result["compiled_artifacts"]
        == inputs["inventory"]["compiled_artifacts"]
    )


def test_rejects_credential_values_in_source_preparation(inputs):
    inputs["foundations"]["source_credentials"][
        "sandbox-oracle"
    ]["password"] = "example-only"

    with pytest.raises(
        ConfigurationError,
        match="must not contain credential values",
    ):
        build_deployment_inputs(**inputs)


def test_cli_writes_once_and_does_not_expose_references(
    inputs,
    tmp_path,
    capsys,
):
    args = ["prepare-deployment"]

    for name, value in inputs.items():
        if name == "compiler_commit":
            continue

        path = tmp_path / f"{name}.json"

        path.write_text(
            json.dumps(value),
            encoding="utf-8",
        )

        args.extend([
            f"--{name.replace('_', '-')}",
            str(path),
        ])

    output = tmp_path / "deployment.generated.auto.tfvars.json"

    args.extend([
        "--compiler-commit",
        COMMIT,
        "--output",
        str(output),
    ])

    assert main(args) == 0

    original = output.read_bytes()

    assert json.loads(original) == build_deployment_inputs(**inputs)

    assert main(args) == 2
    assert output.read_bytes() == original

    logs = capsys.readouterr()

    assert ACCOUNT not in logs.out + logs.err


def test_new_generation_preserves_previous_deployments(inputs):
    inputs["retained_inventory"] = copy.deepcopy(
        inputs["inventory"]
    )

    inputs["retained_assignments"] = copy.deepcopy(
        inputs["capacity_assignments"]
    )

    examples = Path(__file__).resolve().parents[1] / "examples"

    newer = compile_service(
        examples / "postgres.yaml",
        examples / "platform-development.yaml",
        extraction_generation="sandbox-v2",
    )

    inputs["inventory"]["compiled_artifacts"][
        "sandbox-postgres/sandbox-v2"
    ] = newer

    inputs["capacity_assignments"][
        "sandbox-postgres/sandbox-v2"
    ] = {"integration": "sandbox-shared"}

    result = build_deployment_inputs(**inputs)["deployment"]

    assert len(result["compiled_artifacts"]) == 3

    for key, old in inputs["retained_inventory"][
        "compiled_artifacts"
    ].items():
        assert result["compiled_artifacts"][key] == old


def test_rejects_unexpected_capacity_fields(inputs):
    inputs["foundations"]["replication_capacities"][
        "sandbox-shared"
    ]["password"] = "example-only"

    with pytest.raises(
        ConfigurationError,
        match="capacity binding fields",
    ):
        build_deployment_inputs(**inputs)