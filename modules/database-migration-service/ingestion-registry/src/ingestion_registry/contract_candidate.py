import hashlib
import json
import re
from dataclasses import dataclass
from botocore.exceptions import ClientError

from ingestion_registry.identity import TableIdentity
from ingestion_registry.references import S3ObjectReference


class ContractCandidateError(ValueError):
    """Candidate content does not match its expected identity or evidence."""


def _unique_object(pairs):
    result = {}

    for key, value in pairs:
        if key in result:
            raise ContractCandidateError(
                f"Candidate contains a duplicate JSON key: {key}"
            )
        result[key] = value

    return result


def _reject_constant(value):
    raise ContractCandidateError(
        f"Candidate contains an invalid JSON constant: {value}"
    )


@dataclass(frozen=True)
class VerifiedCandidate:
    reference: S3ObjectReference
    contract_version: str
    contract_fingerprint: str
    content: bytes


class ContractCandidateReader:
    def __init__(
        self,
        *,
        s3_client,
        candidate_bucket: str,
        candidate_prefix: str,
        bucket_owner_account_id: str,
        max_bytes: int = 1048576,
    ):
        if (
            not isinstance(candidate_bucket, str)
            or not candidate_bucket
            or candidate_bucket != candidate_bucket.strip()
            or "/" in candidate_bucket
        ):
            raise ValueError(
                "candidate_bucket must be an explicit bucket name"
            )

        if (
            not isinstance(candidate_prefix, str)
            or not candidate_prefix.endswith("/")
            or candidate_prefix != candidate_prefix.strip()
            or any(
                part in ("", ".", "..")
                for part in candidate_prefix[:-1].split("/")
            )
        ):
            raise ValueError(
                "candidate_prefix must contain explicit path "
                "segments and end with '/'"
            )

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
            not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or not 1 <= max_bytes <= 1048576
        ):
            raise ValueError(
                "max_bytes must be between 1 and 1048576"
            )

        self.s3 = s3_client
        self.candidate_bucket = candidate_bucket
        self.candidate_prefix = candidate_prefix
        self.bucket_owner_account_id = bucket_owner_account_id
        self.max_bytes = max_bytes

    def recover(
        self,
        *,
        table: TableIdentity,
        contract_version: str,
        request_id: str,
        kms_key_arn: str,
    ) -> VerifiedCandidate | None:
        if not isinstance(table, TableIdentity):
            raise ValueError("table must be a TableIdentity")

        if (
            not isinstance(contract_version, str)
            or re.fullmatch(
                r"v[1-9][0-9]*",
                contract_version,
            ) is None
        ):
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

        identity = {
            "source": table.source,
            "database_name": table.database_name,
            "schema_name": table.schema_name,
            "table_name": table.table_name,
            "contract_version": contract_version,
        }

        # This is the version-1 candidate storage key protocol.
        # Keep it consistent with schema-generation/candidate_storage.py.
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

        digest = hashlib.sha256(request_identity).hexdigest()
        object_key = f"candidates/{digest}/contract.avsc"

        if not object_key.startswith(self.candidate_prefix):
            raise ContractCandidateError(
                "Candidate recovery is outside the configured location"
            )

        try:
            response = self.s3.get_object(
                Bucket=self.candidate_bucket,
                Key=object_key,
                ExpectedBucketOwner=self.bucket_owner_account_id,
            )
        except ClientError as error:
            # Access failures and other AWS errors must propagate.
            # They must never be interpreted as an absent candidate.
            if error.response.get("Error", {}).get("Code") == "NoSuchKey":
                return None
            raise

        body = response["Body"]

        try:
            version_id = response.get("VersionId")

            if (
                not isinstance(version_id, str)
                or not version_id
                or version_id != version_id.strip()
                or version_id == "null"
            ):
                raise ContractCandidateError(
                    "Candidate recovery requires a versioned object"
                )

            if (
                response.get("ServerSideEncryption") != "aws:kms"
                or response.get("SSEKMSKeyId") != kms_key_arn
            ):
                raise ContractCandidateError(
                    "Recovered candidate encryption does not match configuration"
                )

            raw = body.read(self.max_bytes + 1)
        finally:
            body.close()

        if len(raw) > self.max_bytes:
            raise ContractCandidateError(
                "Candidate exceeds the configured size limit"
            )

        reference = S3ObjectReference(
            bucket_name=self.candidate_bucket,
            object_key=object_key,
            version_id=version_id,
            sha256=hashlib.sha256(raw).hexdigest(),
        )

        # Discovery finds the current object. Verification then reads
        # its exact immutable version and checks the selected identity.
        return self.read(
            table=table,
            contract_version=contract_version,
            reference=reference,
        )

    def read(
        self,
        *,
        table: TableIdentity,
        contract_version: str,
        reference: S3ObjectReference,
    ) -> VerifiedCandidate:
        if not isinstance(table, TableIdentity):
            raise ValueError("table must be a TableIdentity")

        if (
            not isinstance(contract_version, str)
            or re.fullmatch(r"v[1-9][0-9]*", contract_version) is None
        ):
            raise ValueError("contract_version must use v1, v2, etc.")

        if not isinstance(reference, S3ObjectReference):
            raise ValueError(
                "reference must be an S3ObjectReference"
            )

        if (
            reference.bucket_name != self.candidate_bucket
            or not reference.object_key.startswith(
                self.candidate_prefix
            )
        ):
            raise ContractCandidateError(
                "Candidate is outside the configured candidate location"
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
                raise ContractCandidateError(
                    "S3 returned a different candidate object version"
                )

            raw = body.read(self.max_bytes + 1)
        finally:
            body.close()

        if len(raw) > self.max_bytes:
            raise ContractCandidateError(
                "Candidate exceeds the configured size limit"
            )

        if hashlib.sha256(raw).hexdigest() != reference.sha256:
            raise ContractCandidateError(
                "Candidate bytes do not match the approved object checksum"
            )

        try:
            contract = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ContractCandidateError(
                "Candidate is not valid UTF-8 JSON"
            ) from error

        if not isinstance(contract, dict):
            raise ContractCandidateError(
                "Candidate must be a JSON object"
            )

        expected = {
            "source": table.source,
            "database_name": table.database_name,
            "schema_name": table.schema_name,
            "table_name": table.table_name,
            "contract_version": contract_version,
        }

        for field, value in expected.items():
            if contract.get(field) != value:
                raise ContractCandidateError(
                    f"Candidate {field} does not match the selected contract"
                )

        normalised = json.dumps(
            contract,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")

        return VerifiedCandidate(
            reference=reference,
            contract_version=contract_version,
            contract_fingerprint=hashlib.sha256(
                normalised
            ).hexdigest(),
            content=raw,
        )