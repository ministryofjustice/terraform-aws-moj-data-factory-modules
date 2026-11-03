import hashlib
import re
from dataclasses import asdict

from ingestion_config.compiler import canonical_json
from ingestion_config.deployment_bundle import build_deployment_bundle
from ingestion_config.json_documents import MAX_JSON_BYTES
from ingestion_config.loader import ConfigurationError
from ingestion_config.published_documents import DocumentReference


def publish_deployments(
    deployments,
    *,
    client,
    account_id,
    bucket_name,
    kms_key_arn,
):
    if not re.fullmatch(r"[0-9]{12}", account_id):
        raise ConfigurationError(
            "Expected a 12-digit deployment account ID"
        )

    if not re.fullmatch(
        r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]",
        bucket_name,
    ):
        raise ConfigurationError(
            "Expected a configuration bucket name"
        )

    if not re.fullmatch(
        rf"arn:aws:kms:eu-west-2:{account_id}:key/[A-Za-z0-9-]+",
        kms_key_arn,
    ):
        raise ConfigurationError(
            "Configuration key must belong to the deployment account and region"
        )

    # Validate every deployment before uploading any documents
    bundle = build_deployment_bundle(
        deployments,
        account_id=account_id,
    )

    pending = []

    for name, service in sorted(bundle["services"].items()):
        source = service["artifact"]["runtime_configuration"]["source"]

        if not re.fullmatch(r"[a-z][a-z0-9-]{0,29}", source):
            raise ConfigurationError(
                "Invalid publication service name"
            )

        for kind in ("artifact", "manifest"):
            raw = canonical_json(service[kind]).encode("utf-8")

            if len(raw) > MAX_JSON_BYTES:
                raise ConfigurationError(
                    "Published JSON document must not exceed 4 MiB"
                )

            digest = hashlib.sha256(raw).hexdigest()
            key = f"services/{source}/{kind}/{digest}.json"

            pending.append(
                (name, kind, key, digest, raw)
            )

    location = {
        "Bucket": bucket_name,
        "ExpectedBucketOwner": account_id,
    }

    versioning = client.get_bucket_versioning(**location)

    if versioning.get("Status") != "Enabled":
        raise ConfigurationError(
            "Configuration bucket versioning must be enabled"
        )

    references = {
        name: {}
        for name in bundle["services"]
    }

    for name, kind, key, digest, raw in pending:
        request = {
            **location,
            "Key": key,
        }

        try:
            uploaded = client.put_object(
                **request,
                Body=raw,
                IfNoneMatch="*",
                ContentType="application/json",
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId=kms_key_arn,
            )

        except Exception as error:
            code = (
                getattr(error, "response", {})
                .get("Error", {})
                .get("Code")
            )

            if code not in ("PreconditionFailed", "412"):
                raise

            uploaded = client.head_object(**request)

        version = uploaded.get("VersionId")

        if (
            not isinstance(version, str)
            or not version.strip()
            or version == "null"
        ):
            raise ConfigurationError(
                "Published document has no usable S3 version"
            )

        reference = DocumentReference(
            bucket_name,
            key,
            version,
            digest,
        )

        response = client.get_object(
            **request,
            VersionId=version,
        )

        body = response["Body"]

        try:
            if response.get("VersionId") != version:
                raise ConfigurationError(
                    "S3 returned a different document version"
                )

            if (
                response.get("ServerSideEncryption") != "aws:kms"
                or response.get("SSEKMSKeyId") != kms_key_arn
            ):
                raise ConfigurationError(
                    "Published document encryption does not match"
                )

            downloaded = body.read(MAX_JSON_BYTES + 1)

            if downloaded != raw:
                raise ConfigurationError(
                    "Published document content does not match"
                )

        finally:
            body.close()

        references[name][kind] = asdict(reference)

    return {
        "publication_version": 1,
        "environment": bundle["environment"],
        "account_id": account_id,
        "region": bundle["region"],
        "services": references,
    }


def publish_deployments_with_aws(
    deployments,
    *,
    account_id,
    bucket_name,
    kms_key_arn,
):
    try:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ClientError

    except ImportError as error:
        raise ConfigurationError(
            "Publication requires moj-ingestion-config[aws]"
        ) from error

    config = Config(
        retries={
            "mode": "standard",
            "total_max_attempts": 3,
        },
        connect_timeout=5,
        read_timeout=60,
    )

    try:
        session = boto3.session.Session(
            region_name="eu-west-2"
        )

        identity = session.client(
            "sts",
            config=config,
        ).get_caller_identity()

        if identity.get("Account") != account_id:
            raise ConfigurationError(
                "AWS credentials do not match the deployment account"
            )

        return publish_deployments(
            deployments,
            client=session.client("s3", config=config),
            account_id=account_id,
            bucket_name=bucket_name,
            kms_key_arn=kms_key_arn,
        )

    except (BotoCoreError, ClientError) as error:
        raise ConfigurationError(
            "AWS document publication failed; "
            "check permissions and publication settings"
        ) from error