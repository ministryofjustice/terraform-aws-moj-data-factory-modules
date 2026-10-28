import argparse
import base64
import hashlib
import json
import os
import re
import zipfile
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


REPOSITORY = "ministryofjustice/terraform-aws-moj-data-factory-modules"
ARCHIVE = "dms-runtime-python312-x86_64.zip"
PREFIX = "lambda/dms-runtime-controller"
HANDLER = "dms_runtime.lambda_function.lambda_handler"


def sha256(path: Path) -> bytes:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").digest()


def verify_artifact(directory: Path, expected: dict[str, str]) -> dict:
    archive = directory / ARCHIVE
    metadata_path = directory / "artifact-metadata.json"
    checksum_path = directory / "SHA256SUMS"

    for path in (archive, metadata_path, checksum_path):
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Missing or invalid artifact file: {path.name}")

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    required = {
        **expected,
        "source_repository": REPOSITORY,
        "archive": ARCHIVE,
        "python_version": "3.12",
        "architecture": "x86_64",
    }

    for name, value in required.items():
        if metadata.get(name) != value:
            raise ValueError(f"Artifact metadata mismatch: {name}")

    if re.fullmatch(r"[0-9a-f]{40}", metadata["source_commit"]) is None:
        raise ValueError("Invalid source commit")

    for name in ("workflow_run_id", "workflow_run_attempt"):
        if re.fullmatch(r"[1-9][0-9]*", metadata[name]) is None:
            raise ValueError(f"Invalid {name}")

    digest = sha256(archive).hex()

    if metadata.get("sha256") != digest:
        raise ValueError("ZIP checksum does not match artifact metadata")

    expected_checksums = f"{digest}  {ARCHIVE}\n"

    if checksum_path.read_text(encoding="utf-8") != expected_checksums:
        raise ValueError("SHA256SUMS does not match the ZIP")

    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip() is not None:
            raise ValueError("ZIP integrity check failed")

        build = json.loads(bundle.read("runtime-build.json"))

    for name, value in {
        "python_version": "3.12",
        "architecture": "x86_64",
        "handler": HANDLER,
    }.items():
        if build.get(name) != value:
            raise ValueError(f"Runtime build metadata mismatch: {name}")

    return metadata


def publish_file(
    client,
    *,
    path: Path,
    bucket: str,
    key: str,
    kms_key_arn: str,
    account_id: str,
) -> dict:
    digest = sha256(path)
    encoded_digest = base64.b64encode(digest).decode("ascii")

    arguments = {
        "Bucket": bucket,
        "Key": key,
        "ExpectedBucketOwner": account_id,
    }

    try:
        with path.open("rb") as stream:
            response = client.put_object(
                **arguments,
                Body=stream,
                IfNoneMatch="*",
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId=kms_key_arn,
                ChecksumAlgorithm="SHA256",
                ChecksumSHA256=encoded_digest,
                ContentType=(
                    "application/zip"
                    if path.suffix == ".zip"
                    else "application/json"
                    if path.suffix == ".json"
                    else "text/plain"
                ),
            )
    except ClientError as error:
        if error.response["Error"]["Code"] != "PreconditionFailed":
            raise

        response = client.head_object(**arguments)

    version_id = response.get("VersionId")

    if not version_id or version_id == "null":
        raise ValueError(f"Object has no usable S3 version: {key}")

    downloaded = client.get_object(
        **arguments,
        VersionId=version_id,
    )

    body = downloaded["Body"]

    try:
        remote_digest = hashlib.sha256()

        for chunk in body.iter_chunks(chunk_size=1024 * 1024):
            remote_digest.update(chunk)
    finally:
        body.close()

    if remote_digest.digest() != digest:
        raise ValueError(f"S3 object content mismatch: {key}")

    if (
        downloaded.get("ServerSideEncryption") != "aws:kms"
        or downloaded.get("SSEKMSKeyId") != kms_key_arn
    ):
        raise ValueError(f"S3 object encryption mismatch: {key}")

    return {
        "object_key": key,
        "version_id": version_id,
        "sha256": digest.hex(),
        "source_code_hash": encoded_digest,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify and publish the CI-built DMS runtime package"
    )
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()

    expected = {
        "source_commit": os.environ["GITHUB_SHA"],
        "workflow_run_id": os.environ["GITHUB_RUN_ID"],
        "workflow_run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
    }

    if os.environ["GITHUB_REPOSITORY"] != REPOSITORY:
        raise ValueError("Unexpected source repository")

    metadata = verify_artifact(arguments.directory, expected)
    print(f"Verified ZIP SHA-256: {metadata['sha256']}")

    if arguments.verify_only:
        return

    if (
        os.environ["GITHUB_EVENT_NAME"] != "push"
        or os.environ["GITHUB_REF"]
        != "refs/heads/integration/data-hub-modularisation"
    ):
        raise ValueError("Publishing requires a push to the integration branch")

    if arguments.output is None:
        raise ValueError("--output is required when publishing")

    bucket = os.environ["ARTIFACT_BUCKET_NAME"]
    kms_key_arn = os.environ["ARTIFACT_KMS_KEY_ARN"]

    match = re.fullmatch(
        r"arn:aws:kms:eu-west-2:([0-9]{12}):key/[A-Za-z0-9-]+",
        kms_key_arn,
    )

    if match is None:
        raise ValueError("Expected a development artifact key ARN in eu-west-2")

    account_id = match.group(1)
    session = boto3.session.Session(region_name="eu-west-2")

    config = Config(
        retries={"mode": "standard", "total_max_attempts": 3},
        connect_timeout=5,
        read_timeout=60,
    )

    identity = session.client("sts", config=config).get_caller_identity()

    if identity["Account"] != account_id:
        raise ValueError("AWS credentials and artifact key accounts differ")

    client = session.client("s3", config=config)

    versioning = client.get_bucket_versioning(
        Bucket=bucket,
        ExpectedBucketOwner=account_id,
    )

    if versioning.get("Status") != "Enabled":
        raise ValueError("Artifact bucket versioning must be enabled")

    prefix = (
        f"{PREFIX}/{metadata['source_commit']}/{metadata['sha256']}"
    )

    published = {}

    for filename, object_name in (
        (ARCHIVE, "package.zip"),
        ("artifact-metadata.json", "artifact-metadata.json"),
        ("SHA256SUMS", "SHA256SUMS"),
    ):
        published[filename] = publish_file(
            client,
            path=arguments.directory / filename,
            bucket=bucket,
            key=f"{prefix}/{object_name}",
            kms_key_arn=kms_key_arn,
            account_id=account_id,
        )

    package = published[ARCHIVE]

    reference = {
        "source_repository": REPOSITORY,
        "source_commit": metadata["source_commit"],
        "workflow_run_id": metadata["workflow_run_id"],
        "workflow_run_attempt": metadata["workflow_run_attempt"],
        "region": "eu-west-2",
        "python_version": "3.12",
        "architecture": "x86_64",
        "handler": HANDLER,
        "bucket_name": bucket,
        "kms_key_arn": kms_key_arn,
        **package,
        "published_files": published,
    }

    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(reference, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print("Published and verified the runtime package and supporting files")
    print(f"Deployment reference saved to: {arguments.output}")


if __name__ == "__main__":
    main()