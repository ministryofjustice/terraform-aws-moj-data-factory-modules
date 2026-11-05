import hashlib
import json
import re
from dataclasses import dataclass

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