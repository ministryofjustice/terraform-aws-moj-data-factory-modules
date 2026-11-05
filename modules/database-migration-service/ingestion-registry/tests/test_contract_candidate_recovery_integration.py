import io
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from ingestion_registry.contract_candidate import (
    ContractCandidateError,
    ContractCandidateReader,
)

from test_candidate_generation_integration import (
    candidate_generation as candidate_generation,
    generation_adapter as generation_adapter,
    generate_through_adapter,
)


def recovery_reader(setup):
    return ContractCandidateReader(
        s3_client=setup["s3"],
        candidate_bucket=setup["candidate"].bucket_name,
        candidate_prefix="candidates/",
        bucket_owner_account_id="123456789012",
    )


def recover(setup, reader, **changes):
    arguments = {
        "table": setup["table"],
        "contract_version": setup["reservation"]["contract_version"],
        "request_id": setup["reservation"]["request_id"],
        "kms_key_arn": setup["kms_key"],
    }
    arguments.update(changes)

    return reader.recover(**arguments)


def test_missing_candidate_returns_none(candidate_generation):
    setup = candidate_generation

    assert recover(setup, recovery_reader(setup)) is None


def test_recovery_matches_actual_generation_storage(
    generation_adapter,
):
    setup = generation_adapter
    generated = generate_through_adapter(setup)

    recovered = recover(setup, recovery_reader(setup))

    assert recovered is not None
    assert recovered.contract_version == "v2"
    assert recovered.contract_fingerprint == (
        generated["contract_fingerprint"]
    )
    assert recovered.reference.bucket_name == (
        generated["candidate_reference"]["bucket_name"]
    )
    assert recovered.reference.object_key == (
        generated["candidate_reference"]["object_key"]
    )
    assert recovered.reference.version_id == (
        generated["candidate_reference"]["version_id"]
    )
    assert recovered.reference.sha256 == (
        generated["candidate_reference"]["sha256"]
    )


def test_candidate_is_recoverable_after_lost_lambda_response(
    generation_adapter,
):
    setup = generation_adapter

    def invoke_then_lose_response(**kwargs):
        response = setup["invoke_handler"](**kwargs)
        response["Payload"].close()

        raise ReadTimeoutError(
            endpoint_url="https://lambda.eu-west-2.amazonaws.com"
        )

    setup["invocation"].invoke.side_effect = (
        invoke_then_lose_response
    )

    with pytest.raises(ReadTimeoutError):
        generate_through_adapter(setup)

    setup["invocation"].invoke.reset_mock()
    setup["secret_lookup"].reset_mock()

    recovered = recover(setup, recovery_reader(setup))

    assert recovered is not None
    assert recovered.contract_version == "v2"

    setup["invocation"].invoke.assert_not_called()
    setup["secret_lookup"].assert_not_called()


def test_recovery_rejects_unexpected_encryption(
    generation_adapter,
):
    setup = generation_adapter
    generate_through_adapter(setup)

    with pytest.raises(ContractCandidateError, match="encryption"):
        recover(
            setup,
            recovery_reader(setup),
            kms_key_arn=(
                "arn:aws:kms:eu-west-2:123456789012:"
                "key/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            ),
        )


def test_access_denied_is_not_treated_as_missing(
    candidate_generation,
):
    setup = candidate_generation
    reader = recovery_reader(setup)
    denied = ClientError(
        {
            "Error": {
                "Code": "AccessDenied",
                "Message": "Denied",
            },
            "ResponseMetadata": {"HTTPStatusCode": 403},
        },
        "GetObject",
    )

    reader.s3 = Mock()
    reader.s3.get_object.side_effect = denied

    with pytest.raises(ClientError) as raised:
        recover(setup, reader)

    assert raised.value is denied


def test_unversioned_candidate_is_rejected_and_body_closed(
    candidate_generation,
):
    setup = candidate_generation
    reader = recovery_reader(setup)
    body = io.BytesIO(b"{}")

    reader.s3 = Mock()
    reader.s3.get_object.return_value = {
        "Body": body,
        "VersionId": "null",
        "ServerSideEncryption": "aws:kms",
        "SSEKMSKeyId": setup["kms_key"],
    }

    with pytest.raises(ContractCandidateError, match="versioned"):
        recover(setup, reader)

    assert body.closed


def test_oversized_candidate_is_rejected_and_body_closed(
    candidate_generation,
):
    setup = candidate_generation
    reader = recovery_reader(setup)
    body = io.BytesIO(b"x" * (reader.max_bytes + 1))

    reader.s3 = Mock()
    reader.s3.get_object.return_value = {
        "Body": body,
        "VersionId": "candidate-version",
        "ServerSideEncryption": "aws:kms",
        "SSEKMSKeyId": setup["kms_key"],
    }

    with pytest.raises(ContractCandidateError, match="size limit"):
        recover(setup, reader)

    assert body.closed