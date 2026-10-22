import hashlib
import re
from dataclasses import dataclass
from typing import Any, Protocol

from ingestion_config.json_documents import (
    MAX_JSON_BYTES,
    parse_json_object,
)
from ingestion_config.loader import ConfigurationError


class S3DocumentClient(Protocol):
    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class DocumentReference:
    bucket_name: str
    object_key: str
    version_id: str
    sha256: str

    def __post_init__(self) -> None:
        for field in (
            "bucket_name",
            "object_key",
            "version_id",
            "sha256",
        ):
            value = getattr(self, field)

            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
            ):
                raise ConfigurationError(
                    f"Document reference {field} must be "
                    "a non-empty string without surrounding whitespace"
                )

        if self.version_id == "null":
            raise ConfigurationError(
                "Document reference must identify a versioned "
                "S3 object; the null version is unsupported"
            )

        if not re.fullmatch(r"[a-f0-9]{64}", self.sha256):
            raise ConfigurationError(
                "Document reference sha256 must contain "
                "64 lowercase hexadecimal characters"
            )


def load_published_document(
    client: S3DocumentClient,
    reference: DocumentReference,
    *,
    allowed_bucket: str,
    allowed_prefix: str,
    expected_bucket_owner: str,
) -> dict[str, Any]:
    if (
        not allowed_prefix
        or allowed_prefix.startswith("/")
        or not allowed_prefix.endswith("/")
        or "\\" in allowed_prefix
        or any(
            segment in ("", ".", "..")
            or segment != segment.strip()
            for segment in allowed_prefix[:-1].split("/")
        )
    ):
        raise ConfigurationError(
            "Authorised document prefix must contain explicit "
            "path segments and end with '/'"
        )

    if not re.fullmatch(r"[0-9]{12}", expected_bucket_owner):
        raise ConfigurationError(
            "Expected bucket owner must be a 12-digit AWS account ID"
        )

    if reference.bucket_name != allowed_bucket:
        raise ConfigurationError(
            "Document bucket is outside the authorised storage location"
        )

    if not reference.object_key.startswith(allowed_prefix):
        raise ConfigurationError(
            "Document key is outside the authorised storage prefix"
        )

    if (
        "\\" in reference.object_key
        or any(
            segment in ("", ".", "..")
            or segment != segment.strip()
            for segment in reference.object_key.split("/")
        )
    ):
        raise ConfigurationError(
            "Document key must contain explicit path segments"
        )

    response = client.get_object(
        Bucket=reference.bucket_name,
        Key=reference.object_key,
        VersionId=reference.version_id,
        ExpectedBucketOwner=expected_bucket_owner,
    )

    body = response["Body"]

    try:
        if response.get("VersionId") != reference.version_id:
            raise ConfigurationError(
                "S3 returned a different document version"
            )

        raw = body.read(MAX_JSON_BYTES + 1)

        if len(raw) > MAX_JSON_BYTES:
            raise ConfigurationError(
                "Published JSON document must not exceed 4 MiB"
            )

        if hashlib.sha256(raw).hexdigest() != reference.sha256:
            raise ConfigurationError(
                "Published document SHA-256 does not match "
                "the pinned reference"
            )

        return parse_json_object(
            raw,
            source=(
                f"s3://{reference.bucket_name}/"
                f"{reference.object_key}"
            ),
        )

    finally:
        body.close()