import base64
import copy
import hashlib
import io

import pytest

from ingestion_config.cli import main
from ingestion_config.loader import ConfigurationError
from ingestion_config.runtime_package import verify_runtime_package


ACCOUNT_ID = "111122223333"
REGION = "eu-west-2"
BUCKET = "example-runtime-artifacts"
KMS_KEY_ARN = (
    f"arn:aws:kms:{REGION}:{ACCOUNT_ID}:"
    "key/11111111-1111-1111-1111-111111111111"
)
COMMIT = "a" * 40
VERSION_ID = "package-version-one"
CONTENT = b"example published runtime package"
DIGEST = hashlib.sha256(CONTENT).digest()

OBJECT_KEY = (
    f"lambda/dms-runtime-controller/"
    f"{COMMIT}/{DIGEST.hex()}/package.zip"
)


class FakeS3:
    def __init__(self):
        self.status = "Enabled"
        self.calls = []
        self.body = io.BytesIO(CONTENT)
        self.response = {
            "Body": self.body,
            "VersionId": VERSION_ID,
            "ServerSideEncryption": "aws:kms",
            "SSEKMSKeyId": KMS_KEY_ARN,
            "ContentLength": len(CONTENT),
        }

    def get_bucket_versioning(self, **arguments):
        self.calls.append(("get_bucket_versioning", arguments))
        return {"Status": self.status}

    def get_object(self, **arguments):
        self.calls.append(("get_object", arguments))
        return self.response


@pytest.fixture
def prepared():
    return {
        "deployment": {
            "account_id": ACCOUNT_ID,
            "region": REGION,
            "artifact": {
                "bucket_name": BUCKET,
                "object_key": OBJECT_KEY,
                "version_id": VERSION_ID,
                "source_code_hash": base64.b64encode(DIGEST).decode("ascii"),
            },
        }
    }


def verify(prepared, client):
    return verify_runtime_package(
        prepared,
        s3_client=client,
        expected_account_id=ACCOUNT_ID,
        expected_region=REGION,
        expected_bucket_name=BUCKET,
        expected_kms_key_arn=KMS_KEY_ARN,
    )


def test_verifies_exact_version_and_does_not_change_inputs(prepared):
    original = copy.deepcopy(prepared)
    client = FakeS3()

    result = verify(prepared, client)

    assert prepared == original
    assert result["artifact"] == prepared["deployment"]["artifact"]
    assert result["sha256"] == DIGEST.hex()
    assert result["size_bytes"] == len(CONTENT)
    assert client.body.closed

    assert client.calls == [
        (
            "get_bucket_versioning",
            {
                "Bucket": BUCKET,
                "ExpectedBucketOwner": ACCOUNT_ID,
            },
        ),
        (
            "get_object",
            {
                "Bucket": BUCKET,
                "ExpectedBucketOwner": ACCOUNT_ID,
                "Key": OBJECT_KEY,
                "VersionId": VERSION_ID,
            },
        ),
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("bucket_name", "another-artifact-bucket"),
        ("object_key", "latest/package.zip"),
        ("version_id", "null"),
        ("version_id", ""),
        ("source_code_hash", "incorrect-checksum"),
    ],
)
def test_rejects_invalid_reference_before_s3(prepared, field, value):
    prepared["deployment"]["artifact"][field] = value
    client = FakeS3()

    with pytest.raises(ConfigurationError):
        verify(prepared, client)

    assert client.calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_id", "999900001111"),
        ("region", "eu-west-1"),
    ],
)
def test_rejects_wrong_deployment_context(prepared, field, value):
    prepared["deployment"][field] = value
    client = FakeS3()

    with pytest.raises(ConfigurationError):
        verify(prepared, client)

    assert client.calls == []


def test_rejects_bucket_without_enabled_versioning(prepared):
    client = FakeS3()
    client.status = "Suspended"

    with pytest.raises(ConfigurationError, match="versioning"):
        verify(prepared, client)

    assert [name for name, _ in client.calls] == [
        "get_bucket_versioning"
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("VersionId", "different-version"),
        ("ServerSideEncryption", "AES256"),
        ("SSEKMSKeyId", KMS_KEY_ARN + "-different"),
        ("ContentLength", 0),
        ("ContentLength", len(CONTENT) + 1),
        ("ContentLength", len(CONTENT) - 1),
        ("ContentLength", 251 * 1024 * 1024),
    ],
)
def test_rejects_invalid_s3_response_and_closes_body(
    prepared,
    field,
    value,
):
    client = FakeS3()
    client.response[field] = value

    with pytest.raises(ConfigurationError):
        verify(prepared, client)

    assert client.body.closed


def test_rejects_changed_package_contents(prepared):
    client = FakeS3()
    client.body = io.BytesIO(b"x" * len(CONTENT))
    client.response["Body"] = client.body

    with pytest.raises(ConfigurationError, match="SHA-256"):
        verify(prepared, client)

    assert client.body.closed


def test_rejects_kms_key_from_another_account(prepared):
    client = FakeS3()

    with pytest.raises(ConfigurationError, match="KMS key"):
        verify_runtime_package(
            prepared,
            s3_client=client,
            expected_account_id=ACCOUNT_ID,
            expected_region=REGION,
            expected_bucket_name=BUCKET,
            expected_kms_key_arn=KMS_KEY_ARN.replace(
                ACCOUNT_ID,
                "999900001111",
            ),
        )

    assert client.calls == []


def test_cli_registers_verification_command_without_aws_access():
    with pytest.raises(SystemExit) as result:
        main(["verify-runtime-package", "--help"])

    assert result.value.code == 0