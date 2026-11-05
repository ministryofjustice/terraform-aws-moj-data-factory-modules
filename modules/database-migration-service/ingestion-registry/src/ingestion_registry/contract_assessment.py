import hashlib
import json
import re
from dataclasses import asdict

from ingestion_registry.contract_policy import (
    CompatibilityOutcome,
    ContractAssessment,
    ContractPolicy,
    ValidationOutcome,
)
from ingestion_registry.identity import TableIdentity
from ingestion_registry.references import S3ObjectReference


class ContractAssessmentBlocked(ValueError):
    """Validation evidence does not match the requested assessment."""


def _unique_object(pairs):
    result = {}

    for key, value in pairs:
        if key in result:
            raise ContractAssessmentBlocked(
                f"Validation evidence contains a duplicate JSON key: {key}"
            )
        result[key] = value

    return result


def _reject_constant(value):
    raise ContractAssessmentBlocked(
        f"Validation evidence contains invalid JSON constant: {value}"
    )


class ContractAssessmentAdmission:
    def __init__(
        self,
        *,
        lifecycle,
        candidate_reader,
        current_lookup,
        s3_client,
        evidence_bucket: str,
        evidence_prefix: str,
        bucket_owner_account_id: str,
        assessor_revision: str,
    ):
        if (
            not isinstance(evidence_bucket, str)
            or not evidence_bucket
            or evidence_bucket != evidence_bucket.strip()
            or "/" in evidence_bucket
        ):
            raise ValueError(
                "evidence_bucket must be an explicit bucket name"
            )

        if (
            not isinstance(evidence_prefix, str)
            or not evidence_prefix.endswith("/")
            or evidence_prefix != evidence_prefix.strip()
            or any(
                part in ("", ".", "..")
                for part in evidence_prefix[:-1].split("/")
            )
        ):
            raise ValueError(
                "evidence_prefix must contain explicit path "
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
            not isinstance(assessor_revision, str)
            or not assessor_revision
            or assessor_revision != assessor_revision.strip()
            or len(assessor_revision.encode("utf-8")) > 256
        ):
            raise ValueError(
                "assessor_revision must contain 1 to 256 UTF-8 bytes "
                "without surrounding whitespace"
            )

        self.lifecycle = lifecycle
        self.candidate_reader = candidate_reader
        self.current_lookup = current_lookup
        self.s3 = s3_client
        self.evidence_bucket = evidence_bucket
        self.evidence_prefix = evidence_prefix
        self.bucket_owner_account_id = bucket_owner_account_id
        self.assessor_revision = assessor_revision

    def submit(
        self,
        *,
        table: TableIdentity,
        request_id: str,
        contract_version: str,
        candidate_reference: S3ObjectReference,
        evidence_reference: S3ObjectReference,
        policy: ContractPolicy,
    ) -> dict:
        self.lifecycle._key(table, request_id)

        if not isinstance(policy, ContractPolicy):
            raise ValueError("policy must be a ContractPolicy")

        if not isinstance(evidence_reference, S3ObjectReference):
            raise ValueError(
                "evidence_reference must be an S3ObjectReference"
            )

        if (
            evidence_reference.bucket_name != self.evidence_bucket
            or not evidence_reference.object_key.startswith(
                self.evidence_prefix
            )
        ):
            raise ContractAssessmentBlocked(
                "Validation evidence is outside the trusted location"
            )

        candidate = self.candidate_reader.read(
            table=table,
            contract_version=contract_version,
            reference=candidate_reference,
        )

        current = self.current_lookup.resolve(table=table)
        baseline = (
            None
            if current is None
            else S3ObjectReference(
                **current["selection"]["registered_contract"][
                    "object_reference"
                ]
            )
        )

        response = self.s3.get_object(
            Bucket=evidence_reference.bucket_name,
            Key=evidence_reference.object_key,
            VersionId=evidence_reference.version_id,
            ExpectedBucketOwner=self.bucket_owner_account_id,
        )
        body = response["Body"]

        try:
            if response.get("VersionId") != evidence_reference.version_id:
                raise ContractAssessmentBlocked(
                    "Validation evidence object version does not match"
                )
            raw = body.read(65537)
        finally:
            body.close()

        if len(raw) > 65536:
            raise ContractAssessmentBlocked(
                "Validation evidence exceeds 64 KiB"
            )

        if hashlib.sha256(raw).hexdigest() != evidence_reference.sha256:
            raise ContractAssessmentBlocked(
                "Validation evidence checksum does not match"
            )

        try:
            document = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ContractAssessmentBlocked(
                "Validation evidence is not valid UTF-8 JSON"
            ) from error

        expected = {
            "validation_result_version": 1,
            "request_id": request_id,
            "identity": {
                "source": table.source,
                "database_name": table.database_name,
                "schema_name": table.schema_name,
                "table_name": table.table_name,
                "contract_version": contract_version,
            },
            "candidate_reference": asdict(candidate.reference),
            "baseline_reference": (
                None if baseline is None else asdict(baseline)
            ),
            "assessor_revision": self.assessor_revision,
        }

        if (
            not isinstance(document, dict)
            or set(document) != set(expected) | {
                "validation", "compatibility"
            }
        ):
            raise ContractAssessmentBlocked(
                "Validation evidence has an unsupported structure"
            )

        actual_context = {
            field: document[field] for field in expected
        }

        def canonical(value):
            return json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )

        if canonical(actual_context) != canonical(expected):
            raise ContractAssessmentBlocked(
                "Validation evidence does not match the exact "
                "request, candidate, baseline and assessor revision"
            )

        try:
            assessment = ContractAssessment(
                validation=ValidationOutcome(document["validation"]),
                compatibility=CompatibilityOutcome(
                    document["compatibility"]
                ),
                candidate_reference=candidate.reference,
                baseline_reference=baseline,
                evidence_reference=evidence_reference,
            )
        except ValueError as error:
            raise ContractAssessmentBlocked(
                "Validation evidence reports unsupported "
                "or inconsistent outcomes"
            ) from error

        return self.lifecycle.submit_assessment(
            table=table,
            request_id=request_id,
            assessment=assessment,
            policy=policy,
        )