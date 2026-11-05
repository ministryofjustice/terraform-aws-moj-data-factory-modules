import copy
import hashlib
import re
from dataclasses import asdict

from botocore.exceptions import ClientError

from ingestion_registry.contract_candidate import ContractCandidateReader
from ingestion_registry.identity import TableIdentity
from ingestion_registry.references import S3ObjectReference


class ContractPromotionConflict(ValueError):
    """The approved candidate conflicts with promotion state or content."""


class ContractPromoter:
    def __init__(
        self,
        *,
        approval_gate,
        candidate_reader: ContractCandidateReader,
        s3_client,
        destination_bucket: str,
        bucket_owner_account_id: str,
        kms_key_arn: str,
    ):
        if (
            not isinstance(destination_bucket, str)
            or not destination_bucket
            or destination_bucket != destination_bucket.strip()
            or "/" in destination_bucket
        ):
            raise ValueError(
                "destination_bucket must be an explicit bucket name"
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

        self.approval_gate = approval_gate
        self.candidate_reader = candidate_reader
        self.s3 = s3_client
        self.destination_bucket = destination_bucket
        self.bucket_owner_account_id = bucket_owner_account_id
        self.kms_key_arn = kms_key_arn

    def promote(
        self,
        *,
        table: TableIdentity,
        request_id: str,
        contract_version: str,
    ) -> dict:
        if not isinstance(table, TableIdentity):
            raise ValueError("table must be a TableIdentity")

        if (
            not isinstance(contract_version, str)
            or re.fullmatch(r"v[1-9][0-9]*", contract_version) is None
        ):
            raise ValueError("contract_version must use v1, v2, etc.")

        authorisation = self.approval_gate.authorise_registration(
            table=table,
            request_id=request_id,
        )

        candidate_reference = S3ObjectReference(
            **authorisation["assessment"]["candidate_reference"]
        )
        candidate = self.candidate_reader.read(
            table=table,
            contract_version=contract_version,
            reference=candidate_reference,
        )

        current = self.approval_gate.authorise_registration(
            table=table,
            request_id=request_id,
        )

        if current != authorisation:
            raise ContractPromotionConflict(
                "Approval changed while reading the candidate"
            )

        object_key = "/".join(
            [
                table.source,
                table.database_name,
                table.schema_name,
                table.table_name,
                contract_version,
                "contract.avsc",
            ]
        )

        try:
            response = self.s3.put_object(
                Bucket=self.destination_bucket,
                Key=object_key,
                Body=candidate.content,
                ContentType="application/avro+json",
                IfNoneMatch="*",
                ExpectedBucketOwner=self.bucket_owner_account_id,
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId=self.kms_key_arn,
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

        request = {
            "Bucket": self.destination_bucket,
            "Key": object_key,
            "ExpectedBucketOwner": self.bucket_owner_account_id,
        }

        if version_id is not None:
            request["VersionId"] = version_id

        response = self.s3.get_object(**request)
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
                raise ContractPromotionConflict(
                    "Promoted contract requires an exact versioned object"
                )

            if (
                response.get("ServerSideEncryption") != "aws:kms"
                or response.get("SSEKMSKeyId") != self.kms_key_arn
            ):
                raise ContractPromotionConflict(
                    "Promoted contract encryption does not match configuration"
                )

            actual = body.read(len(candidate.content) + 1)
        finally:
            body.close()

        if actual != candidate.content:
            raise ContractPromotionConflict(
                "Contract version already contains different bytes; "
                "the existing object was not overwritten"
            )

        final_reference = S3ObjectReference(
            bucket_name=self.destination_bucket,
            object_key=object_key,
            version_id=actual_version,
            sha256=hashlib.sha256(actual).hexdigest(),
        )

        return {
            "status": "PROMOTED",
            "request_key": authorisation["pk"],
            "request_fingerprint": authorisation[
                "request_fingerprint"
            ],
            "approval_revision": int(
                authorisation["approval_revision"]
            ),
            "approval_reference": copy.deepcopy(
                authorisation["approval_reference"]
            ),
            "candidate_reference": asdict(candidate.reference),
            "contract_version": candidate.contract_version,
            "contract_fingerprint": candidate.contract_fingerprint,
            "object_reference": asdict(final_reference),
            "s3_contract_uri": (
                f"s3://{self.destination_bucket}/{object_key}"
            ),
        }