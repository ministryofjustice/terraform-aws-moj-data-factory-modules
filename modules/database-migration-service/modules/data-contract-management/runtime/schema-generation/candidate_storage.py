import hashlib
import json
import re

from botocore.exceptions import ClientError


class CandidateStorageConflict(ValueError):
    """A candidate request already identifies different content."""


def store_candidate(
    *,
    s3_client,
    content: bytes,
    identity: dict,
    request_id: str,
    bucket_name: str,
    bucket_owner_account_id: str,
    kms_key_arn: str,
) -> dict:
    required = {
        "source",
        "database_name",
        "schema_name",
        "table_name",
        "contract_version",
    }

    if (
        not isinstance(identity, dict)
        or set(identity) != required
        or any(
            not isinstance(value, str)
            or not value
            or value != value.strip()
            for value in identity.values()
        )
    ):
        raise ValueError(
            "identity must contain the five non-empty contract identity fields"
        )

    if re.fullmatch(
        r"v[1-9][0-9]*", identity["contract_version"]
    ) is None:
        raise ValueError("contract_version must use v1, v2, etc.")

    if (
        not isinstance(request_id, str)
        or not request_id
        or request_id != request_id.strip()
        or len(request_id.encode("utf-8")) > 256
    ):
        raise ValueError(
            "request_id must contain 1 to 256 UTF-8 bytes "
            "without surrounding whitespace"
        )

    if (
        not isinstance(content, bytes)
        or not 1 <= len(content) <= 1048576
    ):
        raise ValueError(
            "content must contain 1 to 1048576 bytes"
        )

    if (
        not isinstance(bucket_name, str)
        or not bucket_name
        or bucket_name != bucket_name.strip()
        or "/" in bucket_name
    ):
        raise ValueError("bucket_name must be an explicit bucket name")

    if (
        not isinstance(bucket_owner_account_id, str)
        or re.fullmatch(
            r"[0-9]{12}", bucket_owner_account_id
        ) is None
    ):
        raise ValueError(
            "bucket_owner_account_id must contain 12 digits"
        )

    if (
        not isinstance(kms_key_arn, str)
        or re.fullmatch(
            r"arn:aws:kms:[a-z0-9-]+:[0-9]{12}:key/"
            r"[A-Za-z0-9-]+",
            kms_key_arn,
        ) is None
    ):
        raise ValueError(
            "kms_key_arn must be an explicit KMS key ARN"
        )

    request_identity = json.dumps(
        {
            "candidate_request_version": 1,
            "request_id": request_id,
            "identity": identity,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")

    request_digest = hashlib.sha256(request_identity).hexdigest()
    object_key = f"candidates/{request_digest}/contract.avsc"

    try:
        response = s3_client.put_object(
            Bucket=bucket_name,
            Key=object_key,
            Body=content,
            ContentType="application/avro+json",
            IfNoneMatch="*",
            ExpectedBucketOwner=bucket_owner_account_id,
            ServerSideEncryption="aws:kms",
            SSEKMSKeyId=kms_key_arn,
        )
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code")
        status = error.response.get(
            "ResponseMetadata", {}
        ).get("HTTPStatusCode")

        if code not in {"PreconditionFailed", "412"} and status != 412:
            raise

        version_id = None
    else:
        version_id = response.get("VersionId")

    read_request = {
        "Bucket": bucket_name,
        "Key": object_key,
        "ExpectedBucketOwner": bucket_owner_account_id,
    }

    if version_id is not None:
        read_request["VersionId"] = version_id

    response = s3_client.get_object(**read_request)
    body = response["Body"]

    try:
        actual_version = response.get("VersionId")

        if (
            not isinstance(actual_version, str)
            or not actual_version
            or actual_version != actual_version.strip()
            or actual_version == "null"
            or (
                version_id is not None
                and actual_version != version_id
            )
        ):
            raise CandidateStorageConflict(
                "Candidate storage requires an exact versioned object"
            )

        if (
            response.get("ServerSideEncryption") != "aws:kms"
            or response.get("SSEKMSKeyId") != kms_key_arn
        ):
            raise CandidateStorageConflict(
                "Candidate encryption does not match configuration"
            )

        actual = body.read(len(content) + 1)
    finally:
        body.close()

    if actual != content:
        raise CandidateStorageConflict(
            "Candidate request already contains different bytes; "
            "use a new lifecycle request"
        )

    return {
        "status": "CANDIDATE_STORED",
        "candidate_reference": {
            "bucket_name": bucket_name,
            "object_key": object_key,
            "version_id": actual_version,
            "sha256": hashlib.sha256(actual).hexdigest(),
        },
    }