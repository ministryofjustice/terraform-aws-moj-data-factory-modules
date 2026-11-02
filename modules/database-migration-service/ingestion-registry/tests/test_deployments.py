import copy
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from ingestion_registry.deployments import (
    DeploymentCatalog,
    DeploymentConflict,
)


@pytest.fixture
def entry():
    source = "sandbox-postgres"

    def reference(kind, digest):
        return {
            "bucket_name": "example-ingestion-config",
            "object_key": (
                f"services/{source}/{kind}/{digest}.json"
            ),
            "version_id": "version-one",
            "sha256": digest,
        }

    return {
        "environment": "development",
        "account_id": "111122223333",
        "region": "eu-west-2",
        "source": source,
        "extraction_generation": "sandbox-v1",
        "configuration_revision": "a" * 64,
        "artifact_revision": "b" * 64,
        "artifact_reference": reference("artifact", "c" * 64),
        "manifest_reference": reference("manifest", "d" * 64),
        "allowed_bucket": "example-ingestion-config",
    }


def registry():
    result = Mock()
    result.table_name = "example-registry"
    result._encode.side_effect = copy.deepcopy
    return result


def conflict():
    return ClientError(
        {
            "Error": {
                "Code": "ConditionalCheckFailedException",
            }
        },
        "PutItem",
    )


def test_registers_immutable_reference(entry):
    db = registry()
    original = copy.deepcopy(entry)

    record = DeploymentCatalog(db).register(**entry)

    assert entry == original
    assert record["record_type"] == "DEPLOYMENT_REFERENCE"

    request = db.client.put_item.call_args.kwargs

    assert request["ConditionExpression"] == (
        "attribute_not_exists(pk)"
    )

    assert "status" not in record


def test_identical_registration_can_be_retried(entry):
    db = registry()
    catalog = DeploymentCatalog(db)

    record = catalog.register(**entry)

    db.client.put_item.side_effect = conflict()
    db._read.return_value = record

    assert catalog.register(**entry) == record


def test_changed_reference_is_rejected(entry):
    db = registry()
    catalog = DeploymentCatalog(db)

    record = catalog.register(**entry)

    db.client.put_item.side_effect = conflict()
    db._read.return_value = record

    entry["manifest_reference"]["version_id"] = "another-version"

    with pytest.raises(DeploymentConflict):
        catalog.register(**entry)


def test_rejects_document_from_another_service(entry):
    db = registry()

    entry["manifest_reference"]["object_key"] = (
        entry["manifest_reference"]["object_key"].replace(
            "sandbox-postgres",
            "another-service",
        )
    )

    with pytest.raises(ValueError):
        DeploymentCatalog(db).register(**entry)

    db.client.put_item.assert_not_called()


def test_get_uses_exact_generation_and_revision(entry):
    db = registry()

    identity = {
        name: entry[name]
        for name in (
            "environment",
            "account_id",
            "region",
            "source",
            "extraction_generation",
            "artifact_revision",
        )
    }

    DeploymentCatalog(db).get(**identity)

    db._read.assert_called_once_with(
        "DEPLOYMENT#development#111122223333"
        "#eu-west-2#sandbox-postgres",
        "GENERATION#sandbox-v1#ARTIFACT#" + "b" * 64,
    )


def test_unexpected_aws_failure_is_propagated(entry):
    db = registry()

    db.client.put_item.side_effect = ClientError(
        {
            "Error": {
                "Code": "AccessDeniedException",
            }
        },
        "PutItem",
    )

    with pytest.raises(ClientError):
        DeploymentCatalog(db).register(**entry)

    db._read.assert_not_called()