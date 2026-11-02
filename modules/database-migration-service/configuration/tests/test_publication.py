import io

import pytest

from ingestion_config.loader import ConfigurationError
from ingestion_config.publication import publish_deployments
from test_deployment_bundle import deployments


ACCOUNT = "111122223333"
BUCKET = "example-ingestion-configuration"
KEY = (
    f"arn:aws:kms:eu-west-2:{ACCOUNT}:key/example-key"
)


class AlreadyExists(Exception):
    response = {
        "Error": {
            "Code": "PreconditionFailed",
        }
    }


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.versioning = "Enabled"
        self.attempts = 0
        self.fail_at = None
        self.bodies = []

    def get_bucket_versioning(self, **request):
        assert request["ExpectedBucketOwner"] == ACCOUNT

        return {
            "Status": self.versioning,
        }

    def put_object(self, **request):
        self.attempts += 1

        assert request["ExpectedBucketOwner"] == ACCOUNT
        assert request["IfNoneMatch"] == "*"

        if self.attempts == self.fail_at:
            raise OSError("temporary failure")

        key = request["Key"]

        if key in self.objects:
            raise AlreadyExists()

        self.objects[key] = {
            "raw": request["Body"],
            "VersionId": f"version-{len(self.objects) + 1}",
            "ServerSideEncryption": request["ServerSideEncryption"],
            "SSEKMSKeyId": request["SSEKMSKeyId"],
        }

        return {
            "VersionId": self.objects[key]["VersionId"],
        }

    def head_object(self, **request):
        return {
            "VersionId": self.objects[request["Key"]]["VersionId"],
        }

    def get_object(self, **request):
        assert request["ExpectedBucketOwner"] == ACCOUNT

        item = self.objects[request["Key"]]

        assert request["VersionId"] == item["VersionId"]

        body = io.BytesIO(item["raw"])
        self.bodies.append(body)

        return {
            **item,
            "Body": body,
        }


def publish(deployments, client):
    return publish_deployments(
        deployments,
        client=client,
        account_id=ACCOUNT,
        bucket_name=BUCKET,
        kms_key_arn=KEY,
    )


def test_publishes_two_documents_per_service_and_reuses_versions(
    deployments,
):
    client = FakeS3()

    first = publish(deployments, client)
    second = publish(deployments, client)

    assert first == second
    assert len(client.objects) == 4

    for source, documents in first["services"].items():
        assert set(documents) == {"artifact", "manifest"}

        for kind, reference in documents.items():
            assert reference["object_key"] == (
                f"services/{source}/{kind}/"
                f"{reference['sha256']}.json"
            )

    assert all(body.closed for body in client.bodies)


def test_retry_after_partial_publication_preserves_existing_versions(
    deployments,
):
    client = FakeS3()
    client.fail_at = 3

    with pytest.raises(OSError):
        publish(deployments, client)

    existing = {
        key: item["VersionId"]
        for key, item in client.objects.items()
    }

    assert len(existing) == 2

    client.fail_at = None

    result = publish(deployments, client)

    assert len(result["services"]) == 2
    assert len(client.objects) == 4

    assert all(
        client.objects[key]["VersionId"] == version
        for key, version in existing.items()
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("raw", b"changed"),
        ("SSEKMSKeyId", "wrong-key"),
    ],
)
def test_rejects_conflicting_existing_document(
    deployments,
    field,
    value,
):
    client = FakeS3()

    publish(deployments, client)

    next(iter(client.objects.values()))[field] = value

    with pytest.raises(
        ConfigurationError,
        match="does not match",
    ):
        publish(deployments, client)

    assert all(body.closed for body in client.bodies)


def test_rejects_invalid_deployment_before_writing(deployments):
    client = FakeS3()

    deployments["sandbox-postgres"]["compiled_artifact"][
        "artifact_revision"
    ] = "0" * 64

    with pytest.raises(ConfigurationError):
        publish(deployments, client)

    assert client.attempts == 0


def test_rejects_unversioned_bucket(deployments):
    client = FakeS3()
    client.versioning = "Suspended"

    with pytest.raises(
        ConfigurationError,
        match="versioning",
    ):
        publish(deployments, client)

    assert client.attempts == 0