import json
from pathlib import Path

import pytest

from ingestion_config.compiler import compile_service
from ingestion_config.deployment_bundle import build_deployment_bundle
from ingestion_config.loader import ConfigurationError
from test_deployment_flow import representative_bindings


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
ACCOUNT = "111122223333"


@pytest.fixture
def deployments():
    result = {}

    for engine in ("postgres", "oracle"):
        artifact = compile_service(
            EXAMPLES / f"{engine}.yaml",
            EXAMPLES / "platform-development.yaml",
            extraction_generation="sandbox-v1",
        )

        bindings = representative_bindings(artifact)

        if engine == "oracle":
            bindings = json.loads(
                json.dumps(bindings)
                .replace("INSTANCE1", "INSTANCE2")
                .replace("SOURCE1", "SOURCE2")
                .replace("TARGET1", "TARGET2")
                .replace("TASK1", "TASK2")
            )

        result[artifact["runtime_configuration"]["source"]] = {
            "compiled_artifact": artifact,
            "deployment_bindings": bindings,
        }

    return result


def test_builds_two_validated_manifests(deployments):
    result = build_deployment_bundle(deployments, account_id=ACCOUNT)

    assert set(result["services"]) == {
        "sandbox-postgres",
        "sandbox-oracle",
    }

    assert all(
        item["manifest"]["account_id"] == ACCOUNT
        for item in result["services"].values()
    )


def test_rejects_modified_compiler_artifact(deployments):
    deployments["sandbox-postgres"]["compiled_artifact"][
        "configuration_revision"
    ] = "0" * 64

    with pytest.raises(ConfigurationError, match="revision"):
        build_deployment_bundle(deployments, account_id=ACCOUNT)


def test_rejects_shared_physical_task(deployments):
    postgres = deployments["sandbox-postgres"]["deployment_bindings"][
        "integration"
    ]["replication_tasks"]["integration"]

    deployments["sandbox-oracle"]["deployment_bindings"]["integration"][
        "replication_tasks"
    ]["integration"]["arn"] = postgres["arn"]

    with pytest.raises(ConfigurationError, match="reused"):
        build_deployment_bundle(deployments, account_id=ACCOUNT)


def test_rejects_cross_account_bindings(deployments):
    with pytest.raises(ConfigurationError, match="account|region"):
        build_deployment_bundle(
            deployments,
            account_id="999900001111",
        )

def test_accepts_generation_qualified_deployment_keys(deployments):
    qualified = {
        (
            f"{name}/"
            f"{deployment['compiled_artifact']['extraction_generation']}"
        ): deployment
        for name, deployment in deployments.items()
    }

    result = build_deployment_bundle(
        qualified,
        account_id=ACCOUNT,
    )

    assert set(result["services"]) == set(qualified)