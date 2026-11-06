import hashlib
import json
import re
import uuid
from typing import Any

from ingestion_registry.contracts import RegisteredContract
from ingestion_registry.identity import TableIdentity
from ingestion_registry.references import S3ObjectReference


class ContractNotRegistered(RuntimeError):
    """No registration exists for the requested contract version."""


class ContractVerificationError(ValueError):
    """Registration evidence does not match the selected contract."""


def _reject_constant(value: str) -> None:
    raise ContractVerificationError(
        f"Contract contains an invalid JSON constant: {value}"
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}

    for key, value in pairs:
        if key in result:
            raise ContractVerificationError(
                f"Contract contains a duplicate JSON key: {key}"
            )
        result[key] = value

    return result


class RegisteredContractVerifier:
    def __init__(
        self,
        *,
        contract_table: Any,
        s3_client: Any,
        bucket_name: str,
        bucket_owner_account_id: str,
    ) -> None:
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

        self.contract_table = contract_table
        self.s3 = s3_client
        self.bucket_name = bucket_name
        self.bucket_owner_account_id = bucket_owner_account_id

    def verify(
        self,
        *,
        identity: TableIdentity,
        contract_version: str,
    ) -> RegisteredContract:
        if not isinstance(identity, TableIdentity):
            raise ValueError("identity must be a TableIdentity")

        if (
            not isinstance(contract_version, str)
            or re.fullmatch(r"v[1-9][0-9]*", contract_version) is None
        ):
            raise ValueError("contract_version must use v1, v2, etc.")

        metadata = {
            "source": identity.source,
            "database_name": identity.database_name,
            "schema_name": identity.schema_name,
            "table_name": identity.table_name,
            "contract_version": contract_version,
        }

        contract_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_DNS,
                ":".join(
                    metadata[field]
                    for field in (
                        "source",
                        "database_name",
                        "schema_name",
                        "table_name",
                    )
                ),
            )
        )

        response = self.contract_table.get_item(
            Key={
                "contract_id": contract_id,
                "contract_version": contract_version,
            },
            ConsistentRead=True,
        )
        item = response.get("Item")

        if item is None:
            raise ContractNotRegistered(
                "The selected contract version is not registered"
            )

        if not isinstance(item, dict):
            raise ContractVerificationError(
                "Registration must be an object"
            )

        for field, expected in {
            "contract_id": contract_id,
            **metadata,
        }.items():
            if item.get(field) != expected:
                raise ContractVerificationError(
                    f"Registration {field} does not match the request"
                )

        object_key = "/".join(
            [
                identity.source,
                identity.database_name,
                identity.schema_name,
                identity.table_name,
                contract_version,
                "contract.avsc",
            ]
        )
        expected_uri = f"s3://{self.bucket_name}/{object_key}"

        if item.get("s3_contract_uri") != expected_uri:
            raise ContractVerificationError(
                "Registration S3 URI does not match the permitted "
                "bucket and selected contract path"
            )

        version_id = item.get("s3_object_version_id")

        if (
            not isinstance(version_id, str)
            or not version_id
            or version_id != version_id.strip()
            or version_id == "null"
        ):
            raise ContractVerificationError(
                "Registration requires a versioned S3 object"
            )

        fingerprint = item.get("schema_fingerprint")

        if (
            not isinstance(fingerprint, str)
            or re.fullmatch(r"[a-f0-9]{64}", fingerprint) is None
        ):
            raise ContractVerificationError(
                "Registration requires a valid full-contract fingerprint"
            )

        response = self.s3.get_object(
            Bucket=self.bucket_name,
            Key=object_key,
            VersionId=version_id,
            ExpectedBucketOwner=self.bucket_owner_account_id,
        )
        body = response["Body"]

        try:
            if response.get("VersionId") != version_id:
                raise ContractVerificationError(
                    "S3 returned a different contract object version"
                )

            raw = body.read()
        finally:
            body.close()

        try:
            contract = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ContractVerificationError(
                "Registered contract is not valid UTF-8 JSON"
            ) from error

        if not isinstance(contract, dict):
            raise ContractVerificationError(
                "Registered contract must be a JSON object"
            )

        for field, expected in metadata.items():
            if contract.get(field) != expected:
                raise ContractVerificationError(
                    f"Contract object {field} does not match the request"
                )

        normalised = json.dumps(
            contract,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")

        actual_fingerprint = hashlib.sha256(normalised).hexdigest()

        if actual_fingerprint != fingerprint:
            raise ContractVerificationError(
                "Contract content does not match its registration fingerprint"
            )

        return RegisteredContract(
            contract_id=contract_id,
            contract_version=contract_version,
            contract_fingerprint=actual_fingerprint,
            object_reference=S3ObjectReference(
                bucket_name=self.bucket_name,
                object_key=object_key,
                version_id=version_id,
                sha256=hashlib.sha256(raw).hexdigest(),
            ),
        )