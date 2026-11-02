import copy
from unittest.mock import Mock

import pytest

from ingestion_config.deployment_publication import finalize_deployment
from ingestion_config.loader import ConfigurationError
from test_deployment_bundle import deployments
from test_publication import ACCOUNT, BUCKET, KEY, FakeS3


def finalize(deployments, client, catalog):
    return finalize_deployment(
        deployments,
        s3_client=client,
        catalog=catalog,
        account_id=ACCOUNT,
        bucket_name=BUCKET,
        kms_key_arn=KEY,
    )


def test_verifies_all_documents_before_registering(deployments):
    client = FakeS3()
    catalog = Mock()

    def register(**record):
        assert len(client.objects) == 4
        assert len(client.bodies) == 4
        assert all(body.closed for body in client.bodies)
        return record

    catalog.register.side_effect = register

    result = finalize(deployments, client, catalog)

    assert len(result["services"]) == 2
    assert catalog.register.call_count == 2
    assert result["receipt_version"] == 1


def test_publication_failure_does_not_register_any_service(
    deployments,
):
    client = FakeS3()
    client.fail_at = 3
    catalog = Mock()

    with pytest.raises(OSError):
        finalize(deployments, client, catalog)

    catalog.register.assert_not_called()


def test_registration_retry_reuses_existing_documents(deployments):
    client = FakeS3()
    catalog = Mock()
    registered = {}
    fail = True

    def register(**record):
        if fail and len(registered) == 1:
            raise OSError("temporary Registry failure")

        source = record["source"]

        if source in registered:
            assert registered[source] == record

        registered[source] = copy.deepcopy(record)
        return record

    catalog.register.side_effect = register

    with pytest.raises(OSError):
        finalize(deployments, client, catalog)

    versions = {
        key: item["VersionId"]
        for key, item in client.objects.items()
    }

    fail = False

    result = finalize(deployments, client, catalog)

    assert len(result["services"]) == 2

    assert {
        key: item["VersionId"]
        for key, item in client.objects.items()
    } == versions


def test_modified_artifact_is_rejected_before_aws_writes(
    deployments,
):
    client = FakeS3()
    catalog = Mock()

    deployments["sandbox-postgres"]["compiled_artifact"][
        "artifact_revision"
    ] = "0" * 64

    with pytest.raises(ConfigurationError):
        finalize(deployments, client, catalog)

    assert client.attempts == 0
    catalog.register.assert_not_called()