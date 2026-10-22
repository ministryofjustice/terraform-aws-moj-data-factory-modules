import json
from pathlib import Path
from typing import Any
import hashlib
from io import BytesIO

from ingestion_config.compiler import canonical_json
from ingestion_config.loader import ConfigurationError
from ingestion_config.published_documents import DocumentReference
from ingestion_config.published_resolver import (
    TableLookup,
    resolve_published_table,
)

import pytest

from ingestion_config.cli import main


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
ACCOUNT_ID = "111122223333"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(
    path: Path,
    document: dict[str, Any],
) -> None:
    path.write_text(
        json.dumps(document),
        encoding="utf-8",
    )


def representative_bindings(
    artifact: dict[str, Any],
) -> dict[str, Any]:
    """Represent Terraform output without provisioning AWS resources."""
    bindings = {}

    for number, (group_name, inputs) in enumerate(
        artifact["module_inputs"].items(),
        start=1,
    ):
        prefix = f"arn:aws:dms:eu-west-2:{ACCOUNT_ID}"
        instance = f"{prefix}:rep:INSTANCE{number}"
        source = f"{prefix}:endpoint:SOURCE{number}"
        target = f"{prefix}:endpoint:TARGET{number}"

        task = inputs["replication_tasks"][group_name]

        bindings[group_name] = {
            "deployment_name": inputs["name"],
            "environment": inputs["environment"],
            "replication_instance_arn": instance,
            "source_endpoint": {
                "arn": source,
                "engine": inputs["source_endpoint"]["engine"],
                "database_name": (
                    inputs["source_endpoint"]["database_name"]
                ),
            },
            "target_endpoint": {
                "arn": target,
                "bucket_name": inputs["target"]["bucket_name"],
                "bucket_folder": inputs["target"]["bucket_folder"],
                "cdc_path": None,
            },
            "replication_tasks": {
                group_name: {
                    "arn": f"{prefix}:task:TASK{number}",
                    "id": task["replication_task_id"],
                    "migration_type": task["migration_type"],
                    "replication_instance_arn": instance,
                    "source_endpoint_arn": source,
                    "target_endpoint_arn": target,
                    "table_mappings": json.loads(
                        task["table_mappings"]
                    ),
                }
            },
        }

    return bindings


@pytest.fixture(params=["postgres", "oracle"])
def completed_flow(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> dict[str, Any]:
    artifact_path = tmp_path / "compiled.json"
    bindings_path = tmp_path / "bindings.json"
    manifest_path = tmp_path / "manifest.json"

    assert main(
        [
            "compile",
            "--service",
            str(EXAMPLES / f"{request.param}.yaml"),
            "--platform",
            str(EXAMPLES / "platform-development.yaml"),
            "--extraction-generation",
            "generation-one",
            "--output",
            str(artifact_path),
        ]
    ) == 0

    artifact = read_json(artifact_path)
    bindings = representative_bindings(artifact)
    write_json(bindings_path, bindings)

    assert main(
        [
            "build-manifest",
            "--artifact",
            str(artifact_path),
            "--bindings",
            str(bindings_path),
            "--account-id",
            ACCOUNT_ID,
            "--output",
            str(manifest_path),
        ]
    ) == 0

    manifest = read_json(manifest_path)
    group_name = next(iter(manifest["task_groups"]))
    group = manifest["task_groups"][group_name]
    table = group["tables"][0]

    arguments = [
        "resolve-table",
        "--manifest",
        str(manifest_path),
        "--artifact",
        str(artifact_path),
        "--environment",
        manifest["environment"],
        "--account-id",
        ACCOUNT_ID,
        "--region",
        manifest["region"],
        "--source",
        manifest["source"],
        "--database-name",
        manifest["database_name"],
        "--schema-name",
        table["schema_name"],
        "--table-name",
        table["table_name"],
        "--configuration-revision",
        manifest["configuration_revision"],
        "--artifact-revision",
        manifest["artifact_revision"],
    ]

    return {
        "artifact": artifact,
        "bindings": bindings,
        "manifest": manifest,
        "group_name": group_name,
        "arguments": arguments,
    }


def test_complete_file_based_lookup(
    completed_flow: dict[str, Any],
    tmp_path: Path,
) -> None:
    output = tmp_path / "resolved.json"

    assert main(
        completed_flow["arguments"]
        + ["--output", str(output)]
    ) == 0

    resolved = read_json(output)
    manifest = completed_flow["manifest"]
    group_name = completed_flow["group_name"]
    group = manifest["task_groups"][group_name]

    assert resolved["source"] == manifest["source"]
    assert resolved["configuration_revision"] == (
        manifest["configuration_revision"]
    )
    assert resolved["artifact_revision"] == (
        manifest["artifact_revision"]
    )
    assert resolved["extraction_generation"] == "generation-one"
    assert resolved["landing"] == group["landing"]
    assert resolved["resources"]["replication_task_arn"] == (
        group["replication_task_arn"]
    )
    assert resolved["task_group"]["tables"] == group["tables"]


def test_file_based_lookup_rejects_unselected_table(
    completed_flow: dict[str, Any],
    tmp_path: Path,
    capsys,
) -> None:
    output = tmp_path / "rejected.json"
    arguments = list(completed_flow["arguments"])

    table_position = arguments.index("--table-name") + 1
    arguments[table_position] = "unselected_table"

    assert main(
        arguments + ["--output", str(output)]
    ) == 2

    assert not output.exists()
    assert "not selected" in capsys.readouterr().err


def test_file_based_lookup_rejects_another_revision(
    completed_flow: dict[str, Any],
    tmp_path: Path,
    capsys,
) -> None:
    output = tmp_path / "wrong-revision.json"
    arguments = list(completed_flow["arguments"])

    revision_position = arguments.index("--artifact-revision") + 1
    arguments[revision_position] = "0" * 64

    assert main(
        arguments + ["--output", str(output)]
    ) == 2

    assert not output.exists()
    assert "artifact_revision" in capsys.readouterr().err

class VersionedDocumentStore:
    """Represent versioned S3 reads without accessing AWS."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str, str], bytes] = {}
        self.requests: list[dict[str, Any]] = []

    def publish(
        self,
        key: str,
        version: str,
        document: dict[str, Any],
    ) -> DocumentReference:
        bucket = "example-platform-configuration"
        raw = (canonical_json(document) + "\n").encode("utf-8")

        self.objects[(bucket, key, version)] = raw

        return DocumentReference(
            bucket_name=bucket,
            object_key=key,
            version_id=version,
            sha256=hashlib.sha256(raw).hexdigest(),
        )

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)

        identity = (
            kwargs["Bucket"],
            kwargs["Key"],
            kwargs["VersionId"],
        )

        return {
            "Body": BytesIO(self.objects[identity]),
            "VersionId": kwargs["VersionId"],
        }


@pytest.fixture
def published_flow(
    completed_flow: dict[str, Any],
) -> dict[str, Any]:
    store = VersionedDocumentStore()
    manifest = completed_flow["manifest"]
    artifact = completed_flow["artifact"]
    group = manifest["task_groups"][
        completed_flow["group_name"]
    ]
    table = group["tables"][0]

    prefix = "deployments/development/"

    manifest_reference = store.publish(
        prefix + "service/manifest.json",
        "manifest-version-one",
        manifest,
    )
    artifact_reference = store.publish(
        prefix + "service/artifact.json",
        "artifact-version-one",
        artifact,
    )

    lookup = TableLookup(
        environment=manifest["environment"],
        account_id=ACCOUNT_ID,
        region=manifest["region"],
        source=manifest["source"],
        database_name=manifest["database_name"],
        schema_name=table["schema_name"],
        table_name=table["table_name"],
        configuration_revision=manifest["configuration_revision"],
        artifact_revision=manifest["artifact_revision"],
    )

    return {
        "store": store,
        "manifest_reference": manifest_reference,
        "artifact_reference": artifact_reference,
        "lookup": lookup,
        "manifest": manifest,
        "artifact": artifact,
    }


def resolve_published(
    flow: dict[str, Any],
) -> dict[str, Any]:
    return resolve_published_table(
        flow["store"],
        manifest_reference=flow["manifest_reference"],
        artifact_reference=flow["artifact_reference"],
        allowed_bucket="example-platform-configuration",
        allowed_prefix="deployments/development/",
        expected_bucket_owner=ACCOUNT_ID,
        lookup=flow["lookup"],
    )


def test_published_lookup_retains_exact_document_references(
    published_flow: dict[str, Any],
) -> None:
    result = resolve_published(published_flow)

    assert result["document_references"]["manifest"]["version_id"] == (
        "manifest-version-one"
    )
    assert result["document_references"]["artifact"]["version_id"] == (
        "artifact-version-one"
    )
    assert result["document_references"]["manifest"]["sha256"] == (
        published_flow["manifest_reference"].sha256
    )
    assert result["artifact_revision"] == (
        published_flow["artifact"]["artifact_revision"]
    )

    requests = published_flow["store"].requests

    assert [request["VersionId"] for request in requests] == [
        "manifest-version-one",
        "artifact-version-one",
    ]
    assert all(
        request["ExpectedBucketOwner"] == ACCOUNT_ID
        for request in requests
    )


def test_newer_objects_do_not_change_pinned_lookup(
    published_flow: dict[str, Any],
) -> None:
    first = resolve_published(published_flow)
    store = published_flow["store"]

    # Publish different content under the same keys with new versions.
    # The original lookup must continue reading its pinned versions.
    store.publish(
        published_flow["manifest_reference"].object_key,
        "manifest-version-two",
        {"replacement": True},
    )
    store.publish(
        published_flow["artifact_reference"].object_key,
        "artifact-version-two",
        {"replacement": True},
    )

    repeated = resolve_published(published_flow)

    assert repeated == first
    assert [
        request["VersionId"]
        for request in store.requests[-2:]
    ] == [
        "manifest-version-one",
        "artifact-version-one",
    ]


def test_changed_published_bytes_block_lookup(
    published_flow: dict[str, Any],
) -> None:
    reference = published_flow["manifest_reference"]

    published_flow["store"].objects[
        (
            reference.bucket_name,
            reference.object_key,
            reference.version_id,
        )
    ] = b'{"unexpected":"content"}'

    with pytest.raises(ConfigurationError, match="SHA-256"):
        resolve_published(published_flow)

def publish_component_catalog(
    flow: dict[str, Any],
) -> DocumentReference:
    manifest = flow["manifest"]
    table = manifest["task_groups"]["integration"]["tables"][0]

    configured_table = flow["artifact"]["runtime_configuration"][
        "task_groups"
    ]["integration"]["tables"][0]

    catalog = {
        "catalog_version": 1,
        "environment": manifest["environment"],
        "account_id": manifest["account_id"],
        "region": manifest["region"],
        "configuration_revision": manifest["configuration_revision"],
        "artifact_revision": manifest["artifact_revision"],
        "components": {
            "processing": {
                configured_table["processing_profile"]: {
                    "kind": "state_machine",
                    "arn": (
                        "arn:aws:states:eu-west-2:111122223333:"
                        "stateMachine:example-processing"
                    ),
                }
            }
        },
        "contracts": {
            "registration": {
                "kind": "lambda",
                "arn": (
                    "arn:aws:lambda:eu-west-2:111122223333:"
                    "function:example-registration"
                ),
            },
            "registry": {
                "table_name": "example-contract-registry",
                "arn": (
                    "arn:aws:dynamodb:eu-west-2:111122223333:"
                    "table/example-contract-registry"
                ),
            },
            "tables": [
                {
                    "source": manifest["source"],
                    "database_name": manifest["database_name"],
                    "schema_name": table["schema_name"],
                    "table_name": table["table_name"],
                    "generation": {
                        "kind": "lambda",
                        "arn": (
                            "arn:aws:lambda:eu-west-2:111122223333:"
                            "function:example-generation"
                        ),
                    },
                }
            ],
        },
    }

    return flow["store"].publish(
        "deployments/development/service/catalog.json",
        "catalog-version-one",
        catalog,
    )


def resolve_with_catalog(
    flow: dict[str, Any],
    reference: DocumentReference | None,
) -> dict[str, Any]:
    return resolve_published_table(
        flow["store"],
        manifest_reference=flow["manifest_reference"],
        artifact_reference=flow["artifact_reference"],
        allowed_bucket="example-platform-configuration",
        allowed_prefix="deployments/development/",
        expected_bucket_owner=ACCOUNT_ID,
        lookup=flow["lookup"],
        catalog_reference=reference,
        required_components=("processing",),
        required_contract_capabilities=(
            "generation",
            "registration",
            "registry",
        ),
    )


def test_published_lookup_resolves_pinned_capabilities(
    published_flow: dict[str, Any],
) -> None:
    reference = publish_component_catalog(published_flow)

    result = resolve_with_catalog(published_flow, reference)

    assert set(result["component_bindings"]) == {"processing"}
    assert set(result["contract_bindings"]) == {
        "generation",
        "registration",
        "registry",
    }
    assert result["document_references"]["catalog"]["version_id"] == (
        "catalog-version-one"
    )
    assert result["document_references"]["catalog"]["sha256"] == (
        reference.sha256
    )


def test_requested_capabilities_require_catalog(
    published_flow: dict[str, Any],
) -> None:
    with pytest.raises(
        ConfigurationError,
        match="require a pinned component catalog",
    ):
        resolve_with_catalog(published_flow, None)

    assert published_flow["store"].requests == []


def test_recovery_keeps_original_component_catalog(
    published_flow: dict[str, Any],
) -> None:
    reference = publish_component_catalog(published_flow)
    original = resolve_with_catalog(published_flow, reference)

    published_flow["store"].publish(
        reference.object_key,
        "catalog-version-two",
        {"replacement": True},
    )

    repeated = resolve_with_catalog(published_flow, reference)

    assert repeated == original
    assert published_flow["store"].requests[-1]["VersionId"] == (
        "catalog-version-one"
    )


def test_changed_catalog_bytes_block_capability_lookup(
    published_flow: dict[str, Any],
) -> None:
    reference = publish_component_catalog(published_flow)

    published_flow["store"].objects[
        (
            reference.bucket_name,
            reference.object_key,
            reference.version_id,
        )
    ] = b'{"changed":true}'

    with pytest.raises(ConfigurationError, match="SHA-256"):
        resolve_with_catalog(published_flow, reference)