import base64
import hashlib
import re
from typing import Any

from ingestion_config.loader import ConfigurationError


MAX_PACKAGE_BYTES = 250 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024

OBJECT_KEY_PATTERN = re.compile(
    r"lambda/dms-runtime-controller/"
    r"(?P<commit>[0-9a-f]{40})/"
    r"(?P<digest>[0-9a-f]{64})/"
    r"package\.zip"
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigurationError(message)


def verify_runtime_package(
    prepared: dict[str, Any],
    *,
    s3_client: Any,
    expected_account_id: str,
    expected_region: str,
    expected_bucket_name: str,
    expected_kms_key_arn: str,
) -> dict[str, Any]:
    """Read and verify the pinned package without changing AWS resources."""

    _require(
        re.fullmatch(r"[0-9]{12}", expected_account_id) is not None,
        "Expected account ID must contain 12 digits.",
    )

    _require(
        isinstance(expected_region, str)
        and bool(expected_region.strip())
        and expected_region == expected_region.strip(),
        "Expected region must be a non-empty region name.",
    )

    _require(
        isinstance(expected_bucket_name, str)
        and bool(expected_bucket_name.strip())
        and expected_bucket_name == expected_bucket_name.strip(),
        "Expected artifact bucket must be a non-empty bucket name.",
    )

    key_prefix = (
        f"arn:aws:kms:{expected_region}:{expected_account_id}:key/"
    )

    _require(
        isinstance(expected_kms_key_arn, str)
        and expected_kms_key_arn.startswith(key_prefix)
        and re.fullmatch(
            r"[A-Za-z0-9-]+",
            expected_kms_key_arn.removeprefix(key_prefix),
        )
        is not None,
        "Expected artifact KMS key must belong to the deployment account and region.",
    )

    try:
        deployment = prepared["deployment"]
        artifact = deployment["artifact"]

        _require(
            deployment["account_id"] == expected_account_id,
            "Deployment account does not match the expected account.",
        )

        _require(
            deployment["region"] == expected_region,
            "Deployment region does not match the expected region.",
        )

        _require(
            artifact["bucket_name"] == expected_bucket_name,
            "Runtime package does not use the approved artifact bucket.",
        )

        object_key = artifact["object_key"]
        version_id = artifact["version_id"]
        source_code_hash = artifact["source_code_hash"]

        _require(
            isinstance(object_key, str),
            "Runtime package object key must be a string.",
        )

        match = OBJECT_KEY_PATTERN.fullmatch(object_key)

        _require(
            match is not None,
            "Runtime package object key must contain its commit and SHA-256.",
        )

        _require(
            isinstance(version_id, str)
            and bool(version_id.strip())
            and version_id == version_id.strip()
            and version_id != "null",
            "Runtime package must have an explicit S3 version.",
        )

        expected_digest = bytes.fromhex(match["digest"])
        expected_hash = base64.b64encode(expected_digest).decode("ascii")

        _require(
            source_code_hash == expected_hash,
            "Runtime package checksum does not match its object key.",
        )

    except (KeyError, TypeError, ValueError) as error:
        raise ConfigurationError(
            "Prepared deployment has an invalid runtime package reference."
        ) from error

    # ExpectedBucketOwner prevents a request against an unexpected account
    # VersionId prevents silently reading whichever version is currently latest
    arguments = {
        "Bucket": expected_bucket_name,
        "ExpectedBucketOwner": expected_account_id,
    }

    versioning = s3_client.get_bucket_versioning(**arguments)

    _require(
        versioning.get("Status") == "Enabled",
        "Runtime artifact bucket must have versioning enabled.",
    )

    response = s3_client.get_object(
        **arguments,
        Key=object_key,
        VersionId=version_id,
    )

    body = response.get("Body")

    _require(
        body is not None,
        "S3 did not return the runtime package body.",
    )

    try:
        _require(
            response.get("VersionId") == version_id,
            "S3 returned a different runtime package version.",
        )

        _require(
            response.get("ServerSideEncryption") == "aws:kms"
            and response.get("SSEKMSKeyId") == expected_kms_key_arn,
            "Runtime package does not use the approved encryption key.",
        )

        content_length = response.get("ContentLength")

        _require(
            type(content_length) is int
            and 0 < content_length <= MAX_PACKAGE_BYTES,
            "Runtime package size is missing or outside the permitted range.",
        )

        digest = hashlib.sha256()
        bytes_read = 0

        while True:
            chunk = body.read(CHUNK_BYTES)

            if not chunk:
                break

            bytes_read += len(chunk)

            _require(
                bytes_read <= content_length
                and bytes_read <= MAX_PACKAGE_BYTES,
                "Runtime package exceeds its declared or permitted size.",
            )

            digest.update(chunk)

        _require(
            bytes_read == content_length,
            "Runtime package download is incomplete.",
        )

        _require(
            digest.digest() == expected_digest,
            "Runtime package content does not match its SHA-256.",
        )

    finally:
        body.close()

    return {
        "verification_version": 1,
        "account_id": expected_account_id,
        "region": expected_region,
        "artifact": {
            "bucket_name": expected_bucket_name,
            "object_key": object_key,
            "version_id": version_id,
            "source_code_hash": source_code_hash,
        },
        "kms_key_arn": expected_kms_key_arn,
        "sha256": expected_digest.hex(),
        "size_bytes": bytes_read,
    }


def verify_runtime_package_with_aws(
    prepared: dict[str, Any],
    *,
    expected_account_id: str,
    expected_region: str,
    expected_bucket_name: str,
    expected_kms_key_arn: str,
) -> dict[str, Any]:
    """Create AWS clients only when the verification command is invoked."""

    try:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as error:
        raise ConfigurationError(
            "AWS package verification requires the configuration package's "
            "'aws' extra. Install moj-ingestion-config[aws]."
        ) from error

    config = Config(
        retries={
            "mode": "standard",
            "total_max_attempts": 3,
        },
        connect_timeout=5,
        read_timeout=60,
    )

    try:
        session = boto3.session.Session(region_name=expected_region)

        identity = session.client(
            "sts",
            config=config,
        ).get_caller_identity()

        _require(
            identity.get("Account") == expected_account_id,
            "AWS credentials do not belong to the expected deployment account.",
        )

        return verify_runtime_package(
            prepared,
            s3_client=session.client("s3", config=config),
            expected_account_id=expected_account_id,
            expected_region=expected_region,
            expected_bucket_name=expected_bucket_name,
            expected_kms_key_arn=expected_kms_key_arn,
        )

    except (BotoCoreError, ClientError) as error:
        raise ConfigurationError(
            "AWS could not verify the selected runtime package. "
            "Check authentication, read permissions and the package reference."
        ) from error