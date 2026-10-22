import hashlib
from io import BytesIO
from typing import Any

import pytest

from ingestion_config.loader import ConfigurationError
from ingestion_config.published_documents import (
    DocumentReference,
    load_published_document,
)


BUCKET = "example-platform-configuration"
OWNER = "111122223333"
PREFIX = "deployments/development/"


class FakeS3:
    def __init__(
        self,
        raw: bytes,
        version_id: str = "object-version-one",
    ) -> None:
        self.body = BytesIO(raw)
        self.version_id = version_id
        self.requests: list[dict[str, Any]] = []

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)

        return {
            "Body": self.body,
            "VersionId": self.version_id,
        }


def reference_for(
    raw: bytes,
    **overrides: str,
) -> DocumentReference:
    values = {
        "bucket_name": BUCKET,
        "object_key": PREFIX + "service/manifest.json",
        "version_id": "object-version-one",
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    values.update(overrides)

    return DocumentReference(**values)


def load(
    client: FakeS3,
    reference: DocumentReference,
) -> dict[str, Any]:
    return load_published_document(
        client,
        reference,
        allowed_bucket=BUCKET,
        allowed_prefix=PREFIX,
        expected_bucket_owner=OWNER,
    )


def test_reads_exact_version_and_checks_owner() -> None:
    raw = b'{"manifest_version":1}'
    client = FakeS3(raw)
    reference = reference_for(raw)

    assert load(client, reference) == {"manifest_version": 1}
    assert client.requests == [
        {
            "Bucket": BUCKET,
            "Key": reference.object_key,
            "VersionId": "object-version-one",
            "ExpectedBucketOwner": OWNER,
        }
    ]
    assert client.body.closed


def test_changed_content_is_rejected_and_body_closed() -> None:
    original = b'{"value":"original"}'
    client = FakeS3(b'{"value":"changed"}')

    with pytest.raises(ConfigurationError, match="SHA-256"):
        load(client, reference_for(original))

    assert client.body.closed


def test_different_returned_version_is_rejected() -> None:
    raw = b'{"value":1}'
    client = FakeS3(raw, version_id="object-version-two")

    with pytest.raises(ConfigurationError, match="different document version"):
        load(client, reference_for(raw))

    assert client.body.closed


@pytest.mark.parametrize(
    "overrides",
    [
        {"bucket_name": "another-bucket"},
        {"object_key": "deployments/test/manifest.json"},
        {"object_key": PREFIX + "../manifest.json"},
        {"object_key": PREFIX + "service//manifest.json"},
    ],
)
def test_unauthorised_location_is_rejected_before_read(
    overrides: dict[str, str],
) -> None:
    raw = b'{"value":1}'
    client = FakeS3(raw)

    with pytest.raises(ConfigurationError):
        load(client, reference_for(raw, **overrides))

    assert client.requests == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"version_id": ""},
        {"version_id": "null"},
        {"sha256": "invalid"},
    ],
)
def test_invalid_reference_is_rejected(
    overrides: dict[str, str],
) -> None:
    with pytest.raises(ConfigurationError):
        reference_for(b"{}", **overrides)


def test_valid_digest_does_not_allow_duplicate_json_keys() -> None:
    raw = b'{"value":1,"value":2}'
    client = FakeS3(raw)

    with pytest.raises(ConfigurationError, match="Duplicate JSON key"):
        load(client, reference_for(raw))

    assert client.body.closed


def test_service_error_is_available_to_callers() -> None:
    class FailingS3:
        def get_object(self, **kwargs: Any) -> dict[str, Any]:
            raise TimeoutError("S3 request timed out")

    with pytest.raises(TimeoutError, match="timed out"):
        load_published_document(
            FailingS3(),
            reference_for(b"{}"),
            allowed_bucket=BUCKET,
            allowed_prefix=PREFIX,
            expected_bucket_owner=OWNER,
        )