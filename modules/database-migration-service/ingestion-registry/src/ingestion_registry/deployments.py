import copy
import re
from dataclasses import asdict

from botocore.exceptions import ClientError

from ingestion_registry.references import S3ObjectReference


class DeploymentConflict(RuntimeError):
    """A deployment revision already has different document references."""


def _keys(
    *,
    environment,
    account_id,
    region,
    source,
    extraction_generation,
    artifact_revision,
):
    for name, value in (
        ("environment", environment),
        ("region", region),
        ("source", source),
        ("extraction_generation", extraction_generation),
    ):
        if (
            not isinstance(value, str)
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", value)
        ):
            raise ValueError(f"Invalid deployment {name}")

    if not re.fullmatch(r"[0-9]{12}", account_id):
        raise ValueError("Invalid deployment account ID")

    if not re.fullmatch(r"[0-9a-f]{64}", artifact_revision):
        raise ValueError("Invalid deployment artifact revision")

    return {
        "pk": (
            f"DEPLOYMENT#{environment}#{account_id}"
            f"#{region}#{source}"
        ),
        "sk": (
            f"GENERATION#{extraction_generation}"
            f"#ARTIFACT#{artifact_revision}"
        ),
    }


class DeploymentCatalog:
    def __init__(self, registry):
        self.registry = registry

    def register(
        self,
        *,
        environment,
        account_id,
        region,
        source,
        extraction_generation,
        configuration_revision,
        artifact_revision,
        artifact_reference,
        manifest_reference,
        allowed_bucket,
    ):
        keys = _keys(
            environment=environment,
            account_id=account_id,
            region=region,
            source=source,
            extraction_generation=extraction_generation,
            artifact_revision=artifact_revision,
        )

        if not re.fullmatch(
            r"[0-9a-f]{64}",
            configuration_revision,
        ):
            raise ValueError(
                "Invalid deployment configuration revision"
            )

        documents = {}

        for kind, supplied in (
            ("artifact", artifact_reference),
            ("manifest", manifest_reference),
        ):
            reference = S3ObjectReference(**supplied)

            expected_key = (
                f"services/{source}/{kind}/"
                f"{reference.sha256}.json"
            )

            if (
                reference.bucket_name != allowed_bucket
                or reference.object_key != expected_key
            ):
                raise ValueError(
                    "Deployment document is outside its authorised location"
                )

            documents[kind] = asdict(reference)

        record = {
            **keys,
            "record_type": "DEPLOYMENT_REFERENCE",
            "record_version": 1,
            "environment": environment,
            "account_id": account_id,
            "region": region,
            "source": source,
            "extraction_generation": extraction_generation,
            "configuration_revision": configuration_revision,
            "artifact_revision": artifact_revision,
            "document_references": documents,
        }

        try:
            self.registry.client.put_item(
                TableName=self.registry.table_name,
                Item=self.registry._encode(record),
                ConditionExpression="attribute_not_exists(pk)",
            )

        except ClientError as error:
            if (
                error.response["Error"]["Code"]
                != "ConditionalCheckFailedException"
            ):
                raise

            existing = self.registry._read(
                keys["pk"],
                keys["sk"],
            )

            if existing != record:
                raise DeploymentConflict(
                    "Deployment revision already has different "
                    "document references"
                ) from error

        return copy.deepcopy(record)

    def get(self, **identity):
        keys = _keys(**identity)

        return self.registry._read(
            keys["pk"],
            keys["sk"],
        )