import hashlib
import importlib.util
import io
import json
import zipfile
from pathlib import Path
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody


SCRIPT = Path(__file__).resolve().parents[1] / "publish_artifact.py"
SPEC = importlib.util.spec_from_file_location("publish_artifact", SCRIPT)
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)


@pytest.fixture
def artifact(tmp_path):
    archive = tmp_path / publisher.ARCHIVE

    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(
            "runtime-build.json",
            json.dumps(
                {
                    "python_version": "3.12",
                    "architecture": "x86_64",
                    "handler": publisher.HANDLER,
                }
            ),
        )

    expected = {
        "source_commit": "a" * 40,
        "workflow_run_id": "123",
        "workflow_run_attempt": "1",
    }

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    metadata = {
        **expected,
        "source_repository": publisher.REPOSITORY,
        "archive": publisher.ARCHIVE,
        "sha256": digest,
        "python_version": "3.12",
        "architecture": "x86_64",
    }

    (tmp_path / "artifact-metadata.json").write_text(
        json.dumps(metadata), encoding="utf-8"
    )
    (tmp_path / "SHA256SUMS").write_text(
        f"{digest}  {publisher.ARCHIVE}\n", encoding="utf-8"
    )

    return tmp_path, expected


def test_accepts_matching_build(artifact):
    directory, expected = artifact
    result = publisher.verify_artifact(directory, expected)
    assert result["source_commit"] == expected["source_commit"]


def test_rejects_changed_zip(artifact):
    directory, expected = artifact
    with (directory / publisher.ARCHIVE).open("ab") as stream:
        stream.write(b"changed")

    with pytest.raises(ValueError, match="checksum"):
        publisher.verify_artifact(directory, expected)


def test_rejects_another_source_commit(artifact):
    directory, expected = artifact

    with pytest.raises(ValueError, match="source_commit"):
        publisher.verify_artifact(
            directory, {**expected, "source_commit": "b" * 40}
        )


def test_rejects_changed_checksum_file(artifact):
    directory, expected = artifact
    (directory / "SHA256SUMS").write_text("wrong\n", encoding="utf-8")

    with pytest.raises(ValueError, match="SHA256SUMS"):
        publisher.verify_artifact(directory, expected)


@pytest.mark.parametrize("already_exists", [False, True])
def test_publishes_or_reuses_exact_version(tmp_path, already_exists):
    path = tmp_path / "package.zip"
    path.write_bytes(b"tested-package")

    client = Mock()
    key_arn = "arn:aws:kms:eu-west-2:111122223333:key/example"

    if already_exists:
        client.put_object.side_effect = ClientError(
            {"Error": {"Code": "PreconditionFailed"}}, "PutObject"
        )
        client.head_object.return_value = {"VersionId": "version-one"}
    else:
        client.put_object.return_value = {"VersionId": "version-one"}

    client.get_object.return_value = {
        "Body": StreamingBody(io.BytesIO(b"tested-package"), 14),
        "ServerSideEncryption": "aws:kms",
        "SSEKMSKeyId": key_arn,
    }

    result = publisher.publish_file(
        client,
        path=path,
        bucket="example-artifacts",
        key="lambda/dms-runtime-controller/package.zip",
        kms_key_arn=key_arn,
        account_id="111122223333",
    )

    assert result["version_id"] == "version-one"
    assert client.put_object.call_args.kwargs["IfNoneMatch"] == "*"
    assert client.get_object.call_args.kwargs["VersionId"] == "version-one"


def test_rejects_existing_object_with_different_bytes(tmp_path):
    path = tmp_path / "package.zip"
    path.write_bytes(b"expected")

    client = Mock()
    client.put_object.side_effect = ClientError(
        {"Error": {"Code": "PreconditionFailed"}}, "PutObject"
    )
    client.head_object.return_value = {"VersionId": "existing-version"}
    client.get_object.return_value = {
        "Body": StreamingBody(io.BytesIO(b"different"), 9),
    }

    with pytest.raises(ValueError, match="content mismatch"):
        publisher.publish_file(
            client,
            path=path,
            bucket="example-artifacts",
            key="lambda/dms-runtime-controller/package.zip",
            kms_key_arn="example-key",
            account_id="111122223333",
        )


def test_does_not_treat_access_denied_as_existing_object(tmp_path):
    path = tmp_path / "package.zip"
    path.write_bytes(b"expected")

    client = Mock()
    client.put_object.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied"}}, "PutObject"
    )

    with pytest.raises(ClientError):
        publisher.publish_file(
            client,
            path=path,
            bucket="example-artifacts",
            key="lambda/dms-runtime-controller/package.zip",
            kms_key_arn="example-key",
            account_id="111122223333",
        )

    client.head_object.assert_not_called()