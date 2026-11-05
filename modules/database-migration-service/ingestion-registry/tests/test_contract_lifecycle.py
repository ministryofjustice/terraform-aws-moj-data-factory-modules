from dataclasses import replace

import boto3
import pytest
from moto import mock_aws
from unittest.mock import Mock
import hashlib
import io
import json
import copy
import uuid
from dataclasses import asdict

from ingestion_registry.contract_registration import (
    ContractRegistrationBlocked,
)
from ingestion_registry.contract_registration_completion import (
    ContractRegistrationCompletion,
)
from ingestion_registry.contract_approval import (
    ContractApprovalBlocked,
    ContractApprovalGate,
)
from ingestion_registry.contract_lifecycle import ContractReviewConflict
from ingestion_registry.admission import IngestionRegistry
from ingestion_registry.contract_lifecycle import (
    ContractLifecycle,
    ContractRequestConflict,
)
from ingestion_registry.contract_policy import (
    CompatibilityOutcome,
    ContractAssessment,
    ContractPolicy,
    ValidationOutcome,
)
from ingestion_registry.identity import TableIdentity
from ingestion_registry.references import S3ObjectReference


def reference(key, version):
    return S3ObjectReference(
        bucket_name="example-contract-evidence",
        object_key=key,
        version_id=version,
        sha256="a" * 64,
    )


@pytest.fixture
def lifecycle_setup():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="eu-west-2")
        client.create_table(
            TableName="test-contract-lifecycle",
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        registry = IngestionRegistry(
            client,
            table_name="test-contract-lifecycle",
        )
        table = TableIdentity(
            environment="development",
            source="sandbox-postgres",
            database_name="dms_test",
            schema_name="public",
            table_name="dms_integration_test",
        )
        assessment = ContractAssessment(
            validation=ValidationOutcome.VALID,
            compatibility=CompatibilityOutcome.COMPATIBLE,
            candidate_reference=reference(
                "candidates/request-one/contract.avsc",
                "candidate-one",
            ),
            baseline_reference=reference(
                "contracts/baseline.avsc",
                "baseline-one",
            ),
            evidence_reference=reference(
                "validation/request-one/result.json",
                "validation-one",
            ),
        )
        yield registry, ContractLifecycle(registry), table, assessment


def submit(setup, **overrides):
    _, lifecycle, table, assessment = setup
    arguments = {
        "table": table,
        "request_id": "request-one",
        "assessment": assessment,
        "policy": ContractPolicy(revision="policy-v1"),
    }
    arguments.update(overrides)
    return lifecycle.submit_assessment(**arguments)


def test_records_pending_review_without_an_ingestion(lifecycle_setup):
    registry, lifecycle, table, _ = lifecycle_setup

    result = submit(lifecycle_setup)

    assert result["status"] == "PENDING_REVIEW"
    assert result["revision"] == 1
    assert result["assessment"]["candidate_reference"]["version_id"] == (
        "candidate-one"
    )
    assert result["policy"]["revision"] == "policy-v1"
    assert lifecycle.read(
        table=table, request_id="request-one"
    ) == result

    items = registry.client.scan(
        TableName=registry.table_name,
        ConsistentRead=True,
    )["Items"]
    assert len(items) == 1
    assert "ingestion_id" not in result
    assert "ingestion_version" not in result


def test_duplicate_returns_original_record(lifecycle_setup):
    first = submit(lifecycle_setup)
    second = submit(lifecycle_setup)

    assert second == first


@pytest.mark.parametrize(
    "field,replacement",
    [
        (
            "candidate_reference",
            reference("candidates/request-one/contract.avsc", "candidate-two"),
        ),
        (
            "baseline_reference",
            reference("contracts/baseline.avsc", "baseline-two"),
        ),
        (
            "evidence_reference",
            reference("validation/request-one/result.json", "validation-two"),
        ),
    ],
)
def test_request_cannot_be_reused_for_changed_evidence(
    lifecycle_setup, field, replacement
):
    submit(lifecycle_setup)
    changed = replace(
        lifecycle_setup[3],
        **{field: replacement},
    )

    with pytest.raises(ContractRequestConflict):
        submit(lifecycle_setup, assessment=changed)


def test_request_cannot_silently_change_policy(lifecycle_setup):
    submit(lifecycle_setup)

    with pytest.raises(ContractRequestConflict):
        submit(
            lifecycle_setup,
            policy=ContractPolicy(
                revision="policy-v2",
                automatically_accept_compatible=True,
            ),
        )


@pytest.mark.parametrize(
    "compatibility,status",
    [
        (CompatibilityOutcome.NO_CHANGE, "REUSE_PENDING"),
        (CompatibilityOutcome.BREAKING, "PENDING_REVIEW"),
        (CompatibilityOutcome.UNSUPPORTED, "BLOCKED"),
    ],
)
def test_records_required_progression_state(
    lifecycle_setup, compatibility, status
):
    result = submit(
        lifecycle_setup,
        assessment=replace(
            lifecycle_setup[3],
            compatibility=compatibility,
        ),
    )

    assert result["status"] == status


def test_explicit_policy_acceptance_is_not_activation(lifecycle_setup):
    result = submit(
        lifecycle_setup,
        policy=ContractPolicy(
            revision="policy-v2",
            automatically_accept_compatible=True,
        ),
    )

    assert result["status"] == "POLICY_ACCEPTED"
    assert "approval_reference" not in result
    assert "registered_contract" not in result


def test_tables_have_independent_request_namespaces(lifecycle_setup):
    first = submit(lifecycle_setup)
    second = submit(
        lifecycle_setup,
        table=replace(
            lifecycle_setup[2],
            table_name="another_table",
        ),
    )

    assert first["pk"] != second["pk"]


@pytest.mark.parametrize("request_id", ["", " request", None])
def test_invalid_request_id_is_rejected(lifecycle_setup, request_id):
    with pytest.raises(ValueError, match="request_id"):
        submit(lifecycle_setup, request_id=request_id)

def review_arguments(setup, request):
    return {
        "table": setup[2],
        "request_id": "request-one",
        "review_id": "review-one",
        "reviewer": "authenticated-reviewer",
        "outcome": "APPROVED",
        "expected_revision": int(request["revision"]),
        "expected_request_fingerprint": request["request_fingerprint"],
        "evidence_reference": reference(
            "approvals/request-one/review-one.json",
            "approval-one",
        ),
        "authorise": Mock(return_value=None),
    }


@pytest.mark.parametrize("outcome", ["APPROVED", "REJECTED"])
def test_review_updates_request_and_records_evidence(
    lifecycle_setup, outcome
):
    registry, lifecycle, table, _ = lifecycle_setup
    request = submit(lifecycle_setup)
    arguments = review_arguments(lifecycle_setup, request)
    arguments["outcome"] = outcome

    result = lifecycle.record_review(**arguments)

    assert result["status"] == outcome
    assert result["revision"] == 2
    assert result["review"]["reviewer"] == "authenticated-reviewer"
    assert result["review"]["evidence_reference"]["version_id"] == (
        "approval-one"
    )
    arguments["authorise"].assert_called_once_with(
        "authenticated-reviewer", table
    )
    assert lifecycle.read(
        table=table, request_id="request-one"
    ) == result

    items = registry.client.scan(
        TableName=registry.table_name,
        ConsistentRead=True,
    )["Items"]
    assert len(items) == 2


def test_duplicate_review_returns_original_result(lifecycle_setup):
    _, lifecycle, _, _ = lifecycle_setup
    request = submit(lifecycle_setup)
    arguments = review_arguments(lifecycle_setup, request)

    first = lifecycle.record_review(**arguments)
    second = lifecycle.record_review(**arguments)

    assert second == first
    assert arguments["authorise"].call_count == 2


def test_review_id_cannot_be_reused_for_different_outcome(
    lifecycle_setup,
):
    lifecycle = lifecycle_setup[1]
    request = submit(lifecycle_setup)
    arguments = review_arguments(lifecycle_setup, request)
    lifecycle.record_review(**arguments)

    arguments["outcome"] = "REJECTED"

    with pytest.raises(ContractReviewConflict, match="different content"):
        lifecycle.record_review(**arguments)


def test_another_review_cannot_overwrite_completed_review(
    lifecycle_setup,
):
    lifecycle = lifecycle_setup[1]
    request = submit(lifecycle_setup)
    arguments = review_arguments(lifecycle_setup, request)
    lifecycle.record_review(**arguments)

    arguments["review_id"] = "review-two"
    arguments["expected_revision"] = 2
    arguments["outcome"] = "REJECTED"

    with pytest.raises(ContractReviewConflict, match="pending review"):
        lifecycle.record_review(**arguments)


def test_review_rejects_wrong_assessment_fingerprint(lifecycle_setup):
    lifecycle = lifecycle_setup[1]
    request = submit(lifecycle_setup)
    arguments = review_arguments(lifecycle_setup, request)
    arguments["expected_request_fingerprint"] = "b" * 64

    with pytest.raises(ContractReviewConflict, match="exact assessment"):
        lifecycle.record_review(**arguments)

    assert lifecycle.read(
        table=lifecycle_setup[2], request_id="request-one"
    )["status"] == "PENDING_REVIEW"


def test_stale_review_is_rejected(lifecycle_setup):
    lifecycle = lifecycle_setup[1]
    request = submit(lifecycle_setup)
    arguments = review_arguments(lifecycle_setup, request)
    arguments["expected_revision"] = 2

    with pytest.raises(ContractReviewConflict, match="revision changed"):
        lifecycle.record_review(**arguments)


def test_unauthorised_review_does_not_change_request(lifecycle_setup):
    lifecycle = lifecycle_setup[1]
    request = submit(lifecycle_setup)
    arguments = review_arguments(lifecycle_setup, request)
    arguments["authorise"] = Mock(
        side_effect=PermissionError("Reviewer is not permitted")
    )

    with pytest.raises(PermissionError):
        lifecycle.record_review(**arguments)

    assert lifecycle.read(
        table=lifecycle_setup[2], request_id="request-one"
    ) == request


def test_denial_return_value_is_not_treated_as_authorisation(
    lifecycle_setup,
):
    lifecycle = lifecycle_setup[1]
    request = submit(lifecycle_setup)
    arguments = review_arguments(lifecycle_setup, request)
    arguments["authorise"] = Mock(return_value=False)

    with pytest.raises(ValueError, match="return None"):
        lifecycle.record_review(**arguments)


def test_blocked_assessment_cannot_be_approved(lifecycle_setup):
    lifecycle = lifecycle_setup[1]
    request = submit(
        lifecycle_setup,
        assessment=replace(
            lifecycle_setup[3],
            compatibility=CompatibilityOutcome.UNSUPPORTED,
        ),
    )

    with pytest.raises(ContractReviewConflict, match="pending review"):
        lifecycle.record_review(
            **review_arguments(lifecycle_setup, request)
        )

def approved_setup(setup, mutate_document=None):
    lifecycle = setup[1]
    request = submit(setup)
    arguments = review_arguments(setup, request)

    document = {
        "approval_version": 1,
        "table_identity": request["table_identity"],
        "request_id": request["request_id"],
        "request_fingerprint": request["request_fingerprint"],
        "review_id": arguments["review_id"],
        "reviewer": arguments["reviewer"],
        "outcome": "APPROVED",
        "assessment": request["assessment"],
        "policy": request["policy"],
    }

    document = copy.deepcopy(document)
    if mutate_document is not None:
        mutate_document(document)

    raw = json.dumps(document).encode("utf-8")
    arguments["evidence_reference"] = S3ObjectReference(
        bucket_name="example-contract-evidence",
        object_key="approvals/request-one/review-one.json",
        version_id="approval-one",
        sha256=hashlib.sha256(raw).hexdigest(),
    )
    lifecycle.record_review(**arguments)

    body = io.BytesIO(raw)
    s3 = Mock()
    s3.get_object.return_value = {
        "Body": body,
        "VersionId": "approval-one",
    }
    authorise = Mock(return_value=None)
    gate = ContractApprovalGate(
        lifecycle=lifecycle,
        s3_client=s3,
        approval_bucket="example-contract-evidence",
        approval_prefix="approvals/",
        bucket_owner_account_id="123456789012",
        authorise=authorise,
    )
    return gate, s3, body, authorise


def test_approval_gate_verifies_exact_evidence(lifecycle_setup):
    gate, s3, body, authorise = approved_setup(lifecycle_setup)

    result = gate.verify(
        table=lifecycle_setup[2],
        request_id="request-one",
    )

    assert result["expected_revision"] == 2
    assert result["assessment"]["candidate_reference"]["version_id"] == (
        "candidate-one"
    )
    authorise.assert_called_once_with(
        "authenticated-reviewer", lifecycle_setup[2]
    )
    s3.get_object.assert_called_once_with(
        Bucket="example-contract-evidence",
        Key="approvals/request-one/review-one.json",
        VersionId="approval-one",
        ExpectedBucketOwner="123456789012",
    )
    assert body.closed


def test_pending_review_cannot_resume(lifecycle_setup):
    gate, s3, body, _ = approved_setup(lifecycle_setup)

    with pytest.raises(ContractApprovalBlocked, match="does not exist"):
        gate.verify(
            table=lifecycle_setup[2],
            request_id="another-request",
        )

    s3.get_object.assert_not_called()
    body.close()


@pytest.mark.parametrize(
    "field",
    ["candidate_reference", "baseline_reference", "evidence_reference"],
)
def test_approval_must_cover_exact_assessment(lifecycle_setup, field):
    def change(document):
        document["assessment"][field] = {
            **document["assessment"][field],
            "version_id": "different-version",
        }

    gate, _, body, _ = approved_setup(
        lifecycle_setup, mutate_document=change
    )

    with pytest.raises(ContractApprovalBlocked, match="exact"):
        gate.verify(
            table=lifecycle_setup[2],
            request_id="request-one",
        )

    assert body.closed


def test_approval_checksum_mismatch_blocks_resume(lifecycle_setup):
    gate, s3, original_body, _ = approved_setup(lifecycle_setup)
    replacement = io.BytesIO(b"{}")
    s3.get_object.return_value["Body"] = replacement

    with pytest.raises(ContractApprovalBlocked, match="checksum"):
        gate.verify(
            table=lifecycle_setup[2],
            request_id="request-one",
        )

    assert replacement.closed
    original_body.close()


def test_reviewer_permission_is_checked_again(lifecycle_setup):
    gate, s3, body, authorise = approved_setup(lifecycle_setup)
    authorise.side_effect = PermissionError("Permission revoked")

    with pytest.raises(PermissionError):
        gate.verify(
            table=lifecycle_setup[2],
            request_id="request-one",
        )

    s3.get_object.assert_not_called()
    body.close()


def test_approval_from_untrusted_location_is_rejected(lifecycle_setup):
    gate, s3, body, _ = approved_setup(lifecycle_setup)
    gate.approval_prefix = "different-approvals/"

    with pytest.raises(ContractApprovalBlocked, match="trusted location"):
        gate.verify(
            table=lifecycle_setup[2],
            request_id="request-one",
        )

    s3.get_object.assert_not_called()
    body.close()

def test_existing_pending_review_cannot_resume(lifecycle_setup):
    gate, s3, body, _ = approved_setup(lifecycle_setup)

    submit(lifecycle_setup, request_id="pending-request")

    with pytest.raises(
        ContractApprovalBlocked,
        match="not currently approved",
    ):
        gate.verify(
            table=lifecycle_setup[2],
            request_id="pending-request",
        )

    s3.get_object.assert_not_called()
    body.close()

def test_registration_authorisation_records_exact_approval(
    lifecycle_setup,
):
    gate, _, body, _ = approved_setup(lifecycle_setup)

    result = gate.authorise_registration(
        table=lifecycle_setup[2],
        request_id="request-one",
    )

    assert result["approval_revision"] == 2
    assert result["approval_reference"]["version_id"] == "approval-one"
    assert result["assessment"]["candidate_reference"]["version_id"] == (
        "candidate-one"
    )
    assert result["policy"]["revision"] == "policy-v1"
    assert body.closed

    stored = lifecycle_setup[0]._read(
        result["pk"],
        "REGISTRATION_AUTHORIZATION",
    )
    assert stored == result

    current = lifecycle_setup[1].read(
        table=lifecycle_setup[2],
        request_id="request-one",
    )
    assert current["status"] == "APPROVED"
    assert current["revision"] == 2


def test_registration_authorisation_is_idempotent(lifecycle_setup):
    gate, s3, body, _ = approved_setup(lifecycle_setup)
    raw = body.getvalue()

    # Each verification must receive a fresh streaming body.
    s3.get_object.side_effect = lambda **kwargs: {
        "Body": io.BytesIO(raw),
        "VersionId": "approval-one",
    }

    first = gate.authorise_registration(
        table=lifecycle_setup[2],
        request_id="request-one",
    )
    second = gate.authorise_registration(
        table=lifecycle_setup[2],
        request_id="request-one",
    )

    assert first == second
    body.close()


def test_pending_request_cannot_authorise_registration(
    lifecycle_setup,
):
    gate, s3, body, _ = approved_setup(lifecycle_setup)
    request = submit(
        lifecycle_setup,
        request_id="pending-request",
    )

    with pytest.raises(
        ContractApprovalBlocked,
        match="not currently approved",
    ):
        gate.authorise_registration(
            table=lifecycle_setup[2],
            request_id="pending-request",
        )

    assert lifecycle_setup[0]._read(
        request["pk"],
        "REGISTRATION_AUTHORIZATION",
    ) is None
    s3.get_object.assert_not_called()
    body.close()


def test_concurrent_approval_change_blocks_registration_authorisation(
    lifecycle_setup, monkeypatch
):
    gate, _, body, _ = approved_setup(lifecycle_setup)
    registry = lifecycle_setup[0]
    original_verify = gate.verify

    def verify_then_change(**kwargs):
        verified = original_verify(**kwargs)

        registry.client.update_item(
            TableName=registry.table_name,
            Key=registry._encode(
                {
                    "pk": verified["request_key"],
                    "sk": "META",
                }
            ),
            UpdateExpression="SET #revision = :next",
            ExpressionAttributeNames={"#revision": "revision"},
            ExpressionAttributeValues=registry._encode(
                {":next": verified["expected_revision"] + 1}
            ),
        )
        return verified

    monkeypatch.setattr(gate, "verify", verify_then_change)

    with pytest.raises(
        ContractApprovalBlocked,
        match="Approval changed",
    ):
        gate.authorise_registration(
            table=lifecycle_setup[2],
            request_id="request-one",
        )

    request = lifecycle_setup[1].read(
        table=lifecycle_setup[2],
        request_id="request-one",
    )
    assert registry._read(
        request["pk"],
        "REGISTRATION_AUTHORIZATION",
    ) is None
    assert body.closed

def registration_completion_setup(setup):
    gate, s3, body, _ = approved_setup(setup)
    raw = body.getvalue()

    # Each gate verification gets a fresh response body.
    s3.get_object.side_effect = lambda **kwargs: {
        "Body": io.BytesIO(raw),
        "VersionId": "approval-one",
    }

    current = gate.authorise_registration(
        table=setup[2],
        request_id="request-one",
    )
    table = setup[2]

    contract_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_DNS,
            ":".join(
                (
                    table.source,
                    table.database_name,
                    table.schema_name,
                    table.table_name,
                )
            ),
        )
    )
    object_key = "/".join(
        (
            table.source,
            table.database_name,
            table.schema_name,
            table.table_name,
            "v1",
            "contract.avsc",
        )
    )

    result = {
        "status": "REGISTRATION_CONFIRMED",
        "request_key": current["pk"],
        "request_fingerprint": current["request_fingerprint"],
        "approval_revision": int(current["approval_revision"]),
        "approval_reference": current["approval_reference"],
        "candidate_reference": current["assessment"][
            "candidate_reference"
        ],
        "registered_contract": {
            "contract_id": contract_id,
            "contract_version": "v1",
            "contract_fingerprint": "d" * 64,
            "object_reference": asdict(
                S3ObjectReference(
                    bucket_name="example-contracts",
                    object_key=object_key,
                    version_id="registered-one",
                    sha256="a" * 64,
                )
            ),
        },
    }

    registrar = Mock()
    registrar.register.return_value = result
    completion = ContractRegistrationCompletion(
        registrar=registrar,
        approval_gate=gate,
    )
    body.close()
    return completion, registrar, gate


def complete_registration(setup, completion):
    return completion.complete(
        table=setup[2],
        request_id="request-one",
        contract_version="v1",
    )


def test_registration_completion_is_stored_without_activation(
    lifecycle_setup,
):
    completion, registrar, _ = registration_completion_setup(
        lifecycle_setup
    )

    record = complete_registration(lifecycle_setup, completion)

    assert record["record_type"] == "CONTRACT_REGISTRATION"
    assert record["result"]["registered_contract"][
        "object_reference"
    ]["version_id"] == "registered-one"
    registrar.register.assert_called_once_with(
        table=lifecycle_setup[2],
        request_id="request-one",
        contract_version="v1",
    )

    assert lifecycle_setup[0]._read(
        record["pk"], "REGISTRATION"
    ) == record

    current = lifecycle_setup[1].read(
        table=lifecycle_setup[2],
        request_id="request-one",
    )
    assert current["status"] == "APPROVED"
    assert current["revision"] == 2


def test_identical_registration_completion_is_idempotent(
    lifecycle_setup,
):
    completion, _, _ = registration_completion_setup(lifecycle_setup)

    first = complete_registration(lifecycle_setup, completion)
    second = complete_registration(lifecycle_setup, completion)

    assert second == first


def test_different_registration_completion_is_rejected(
    lifecycle_setup,
):
    completion, registrar, _ = registration_completion_setup(
        lifecycle_setup
    )
    complete_registration(lifecycle_setup, completion)

    changed = copy.deepcopy(registrar.register.return_value)
    changed["registered_contract"]["contract_fingerprint"] = "e" * 64
    registrar.register.return_value = changed

    with pytest.raises(
        ContractRegistrationBlocked,
        match="different registration completion",
    ):
        complete_registration(lifecycle_setup, completion)


def test_unconfirmed_registration_creates_no_completion(
    lifecycle_setup,
):
    completion, registrar, _ = registration_completion_setup(
        lifecycle_setup
    )
    registrar.register.return_value["status"] = "ACCEPTED"

    with pytest.raises(
        ContractRegistrationBlocked,
        match="current approval",
    ):
        complete_registration(lifecycle_setup, completion)

    assert lifecycle_setup[0]._read(
        lifecycle_setup[1].read(
            table=lifecycle_setup[2],
            request_id="request-one",
        )["pk"],
        "REGISTRATION",
    ) is None


def test_registration_failure_creates_no_completion(
    lifecycle_setup,
):
    completion, registrar, _ = registration_completion_setup(
        lifecycle_setup
    )
    registrar.register.side_effect = RuntimeError("Registration failed")

    with pytest.raises(RuntimeError, match="Registration failed"):
        complete_registration(lifecycle_setup, completion)

    request = lifecycle_setup[1].read(
        table=lifecycle_setup[2],
        request_id="request-one",
    )
    assert lifecycle_setup[0]._read(
        request["pk"], "REGISTRATION"
    ) is None


def test_concurrent_change_blocks_registration_completion(
    lifecycle_setup, monkeypatch
):
    completion, _, gate = registration_completion_setup(
        lifecycle_setup
    )
    registry = lifecycle_setup[0]
    original_authorise = gate.authorise_registration

    def authorise_then_change(**kwargs):
        result = original_authorise(**kwargs)
        registry.client.update_item(
            TableName=registry.table_name,
            Key=registry._encode(
                {"pk": result["pk"], "sk": "META"}
            ),
            UpdateExpression="SET #revision = :next",
            ExpressionAttributeNames={"#revision": "revision"},
            ExpressionAttributeValues=registry._encode(
                {":next": int(result["approval_revision"]) + 1}
            ),
        )
        return result

    monkeypatch.setattr(
        gate,
        "authorise_registration",
        authorise_then_change,
    )

    with pytest.raises(
        ContractRegistrationBlocked,
        match="Approval changed",
    ):
        complete_registration(lifecycle_setup, completion)

    request = lifecycle_setup[1].read(
        table=lifecycle_setup[2],
        request_id="request-one",
    )
    assert registry._read(request["pk"], "REGISTRATION") is None