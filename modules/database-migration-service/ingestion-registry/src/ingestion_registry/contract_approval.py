import copy
import hashlib
import json

from botocore.exceptions import ClientError
from ingestion_registry.contract_lifecycle import ContractLifecycle
from ingestion_registry.identity import TableIdentity
from ingestion_registry.references import S3ObjectReference


class ContractApprovalBlocked(ValueError):
    """The current request does not have verifiable approval."""


def _unique_object(pairs):
    result = {}

    for key, value in pairs:
        if key in result:
            raise ContractApprovalBlocked(
                "Approval evidence contains duplicate JSON keys"
            )
        result[key] = value

    return result


def _reject_constant(value):
    raise ContractApprovalBlocked(
        f"Approval evidence contains invalid JSON constant: {value}"
    )


class ContractApprovalGate:
    def __init__(
        self,
        *,
        lifecycle: ContractLifecycle,
        s3_client,
        approval_bucket: str,
        approval_prefix: str,
        bucket_owner_account_id: str,
        authorise,
    ):
        if (
            not isinstance(approval_bucket, str)
            or not approval_bucket
            or approval_bucket != approval_bucket.strip()
            or "/" in approval_bucket
        ):
            raise ValueError(
                "approval_bucket must be an explicit bucket name"
            )

        if (
            not isinstance(approval_prefix, str)
            or not approval_prefix.endswith("/")
            or approval_prefix != approval_prefix.strip()
            or any(
                part in ("", ".", "..")
                for part in approval_prefix[:-1].split("/")
            )
        ):
            raise ValueError(
                "approval_prefix must contain explicit path "
                "segments and end with '/'"
            )

        if (
            not isinstance(bucket_owner_account_id, str)
            or len(bucket_owner_account_id) != 12
            or any(
                character not in "0123456789"
                for character in bucket_owner_account_id
            )
        ):
            raise ValueError(
                "bucket_owner_account_id must contain 12 digits"
            )

        if not callable(authorise):
            raise ValueError("authorise must be callable")

        self.lifecycle = lifecycle
        self.s3 = s3_client
        self.approval_bucket = approval_bucket
        self.approval_prefix = approval_prefix
        self.bucket_owner_account_id = bucket_owner_account_id
        self.authorise = authorise

    def verify(
        self,
        *,
        table: TableIdentity,
        request_id: str,
    ) -> dict:
        current = self.lifecycle.read(
            table=table,
            request_id=request_id,
        )

        if current is None:
            raise ContractApprovalBlocked(
                "Contract request does not exist"
            )

        if current.get("status") != "APPROVED":
            raise ContractApprovalBlocked(
                "Contract request is not currently approved"
            )

        review = current.get("review")

        if (
            not isinstance(review, dict)
            or review.get("outcome") != "APPROVED"
            or review.get("expected_request_fingerprint")
            != current.get("request_fingerprint")
            or review.get("expected_revision")
            != current.get("revision") - 1
        ):
            raise ContractApprovalBlocked(
                "Review does not match the current request"
            )

        if self.authorise(review["reviewer"], table) is not None:
            raise ContractApprovalBlocked(
                "Authorisation must raise on denial "
                "and return None on success"
            )

        try:
            reference = S3ObjectReference(
                **review["evidence_reference"]
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ContractApprovalBlocked(
                "Review has an invalid approval evidence reference"
            ) from error

        if (
            reference.bucket_name != self.approval_bucket
            or not reference.object_key.startswith(
                self.approval_prefix
            )
        ):
            raise ContractApprovalBlocked(
                "Approval evidence is outside the trusted location"
            )

        response = self.s3.get_object(
            Bucket=reference.bucket_name,
            Key=reference.object_key,
            VersionId=reference.version_id,
            ExpectedBucketOwner=self.bucket_owner_account_id,
        )
        body = response["Body"]

        try:
            if response.get("VersionId") != reference.version_id:
                raise ContractApprovalBlocked(
                    "Approval object version does not match"
                )

            raw = body.read(65537)
        finally:
            body.close()

        if len(raw) > 65536:
            raise ContractApprovalBlocked(
                "Approval evidence exceeds 64 KiB"
            )

        if hashlib.sha256(raw).hexdigest() != reference.sha256:
            raise ContractApprovalBlocked(
                "Approval evidence checksum does not match"
            )

        try:
            document = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ContractApprovalBlocked(
                "Approval evidence is not valid UTF-8 JSON"
            ) from error

        expected = {
            "approval_version": 1,
            "table_identity": current["table_identity"],
            "request_id": current["request_id"],
            "request_fingerprint": current["request_fingerprint"],
            "review_id": review["review_id"],
            "reviewer": review["reviewer"],
            "outcome": "APPROVED",
            "assessment": current["assessment"],
            "policy": current["policy"],
        }

        def canonical(value):
            return json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )

        if canonical(document) != canonical(expected):
            raise ContractApprovalBlocked(
                "Approval evidence does not cover the exact "
                "assessment, policy and review"
            )

        latest = self.lifecycle.read(
            table=table,
            request_id=request_id,
        )

        if latest != current:
            raise ContractApprovalBlocked(
                "Contract request changed during verification; "
                "read and reconcile the current decision"
            )

        return {
            "request_key": current["pk"],
            "request_fingerprint": current["request_fingerprint"],
            "expected_revision": int(current["revision"]),
            "approval_reference": copy.deepcopy(
                review["evidence_reference"]
            ),
            "assessment": copy.deepcopy(current["assessment"]),
            "policy": copy.deepcopy(current["policy"]),
        }

    def authorise_registration(
        self,
        *,
        table: TableIdentity,
        request_id: str,
    ) -> dict:
        verified = self.verify(
            table=table,
            request_id=request_id,
        )
        registry = self.lifecycle.registry

        record = {
            "pk": verified["request_key"],
            "sk": "REGISTRATION_AUTHORIZATION",
            "record_type": "CONTRACT_REGISTRATION_AUTHORIZATION",
            "request_fingerprint": verified["request_fingerprint"],
            "approval_revision": verified["expected_revision"],
            "approval_reference": verified["approval_reference"],
            "assessment": verified["assessment"],
            "policy": verified["policy"],
        }

        def existing_authorisation():
            existing = registry._read(
                record["pk"],
                record["sk"],
            )

            if existing is None:
                return None

            if existing != record:
                raise ContractApprovalBlocked(
                    "Registration authorisation already exists "
                    "for different approval evidence"
                )

            return copy.deepcopy(existing)

        existing = existing_authorisation()

        actions = [
            {
                "ConditionCheck": {
                    "TableName": registry.table_name,
                    "Key": registry._encode(
                        {
                            "pk": record["pk"],
                            "sk": "META",
                        }
                    ),
                    "ConditionExpression": (
                        "#status = :approved "
                        "AND #revision = :revision "
                        "AND request_fingerprint = :fingerprint "
                        "AND #review.#evidence = :evidence"
                    ),
                    "ExpressionAttributeNames": {
                        "#status": "status",
                        "#revision": "revision",
                        "#review": "review",
                        "#evidence": "evidence_reference",
                    },
                    "ExpressionAttributeValues": registry._encode(
                        {
                            ":approved": "APPROVED",
                            ":revision": verified["expected_revision"],
                            ":fingerprint": verified[
                                "request_fingerprint"
                            ],
                            ":evidence": verified[
                                "approval_reference"
                            ],
                        }
                    ),
                }
            }
        ]

        if existing is None:
            actions.append(registry._put_new(record))

        try:
            registry.client.transact_write_items(
                TransactItems=actions
            )
        except ClientError as error:
            if (
                error.response["Error"]["Code"]
                != "TransactionCanceledException"
            ):
                raise

            reasons = error.response.get(
                "CancellationReasons", []
            )

            if reasons and reasons[0].get("Code") == (
                "ConditionalCheckFailed"
            ):
                raise ContractApprovalBlocked(
                    "Approval changed before registration "
                    "authorisation was recorded"
                ) from error

            if (
                len(reasons) > 1
                and reasons[1].get("Code")
                == "ConditionalCheckFailed"
            ):
                self.verify(
                    table=table,
                    request_id=request_id,
                )
                existing = existing_authorisation()

                if existing is not None:
                    return self.authorise_registration(
                        table=table,
                        request_id=request_id,
                    )

            raise

        return copy.deepcopy(record)