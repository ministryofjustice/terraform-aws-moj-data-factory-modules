import re

from ingestion_config.deployment_bundle import build_deployment_bundle
from ingestion_config.loader import ConfigurationError
from ingestion_config.publication import publish_deployments


def finalize_deployment(
    deployments,
    *,
    s3_client,
    catalog,
    account_id,
    bucket_name,
    kms_key_arn,
):
    bundle = build_deployment_bundle(
        deployments,
        account_id=account_id,
    )

    publication = publish_deployments(
        deployments,
        client=s3_client,
        account_id=account_id,
        bucket_name=bucket_name,
        kms_key_arn=kms_key_arn,
    )

    # Publication returns only after every document has been read back and verified
    registered = {}

    for name, service in sorted(bundle["services"].items()):
        artifact = service["artifact"]
        runtime = artifact["runtime_configuration"]
        references = publication["services"][name]

        registered[name] = catalog.register(
            environment=bundle["environment"],
            account_id=account_id,
            region=bundle["region"],
            source=runtime["source"],
            extraction_generation=artifact["extraction_generation"],
            configuration_revision=artifact["configuration_revision"],
            artifact_revision=artifact["artifact_revision"],
            artifact_reference=references["artifact"],
            manifest_reference=references["manifest"],
            allowed_bucket=bucket_name,
        )

    return {
        "receipt_version": 1,
        "environment": bundle["environment"],
        "account_id": account_id,
        "region": bundle["region"],
        "services": registered,
    }


def finalize_deployment_with_aws(
    deployments,
    *,
    account_id,
    bucket_name,
    kms_key_arn,
    registry_table_arn,
):
    if not re.fullmatch(r"[0-9]{12}", account_id):
        raise ConfigurationError(
            "Expected a 12-digit deployment account ID"
        )

    prefix = (
        f"arn:aws:dynamodb:eu-west-2:{account_id}:table/"
    )

    if not registry_table_arn.startswith(prefix):
        raise ConfigurationError(
            "Registry table must belong to the deployment account and region"
        )

    table_name = registry_table_arn.removeprefix(prefix)

    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", table_name):
        raise ConfigurationError(
            "Invalid Registry table ARN"
        )

    # Reject invalid deployment outputs before accessing AWS
    build_deployment_bundle(
        deployments,
        account_id=account_id,
    )

    try:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ClientError

        from ingestion_registry.admission import IngestionRegistry
        from ingestion_registry.deployments import (
            DeploymentCatalog,
            DeploymentConflict,
        )

    except ImportError as error:
        raise ConfigurationError(
            "Finalization requires moj-ingestion-config[aws] "
            "and the matching ingestion-registry package"
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

        dynamodb = session.client(
            "dynamodb",
            config=config,
        )

        table = dynamodb.describe_table(
            TableName=table_name
        )["Table"]

        if (
            table.get("TableArn") != registry_table_arn
            or table.get("TableStatus") != "ACTIVE"
        ):
            raise ConfigurationError(
                "Expected Registry table is not active"
            )

        registry = IngestionRegistry(
            dynamodb,
            table_name=table_name,
        )

        return finalize_deployment(
            deployments,
            s3_client=session.client("s3", config=config),
            catalog=DeploymentCatalog(registry),
            account_id=account_id,
            bucket_name=bucket_name,
            kms_key_arn=kms_key_arn,
        )

    except DeploymentConflict as error:
        raise ConfigurationError(
            "Deployment reference conflicts with an existing "
            "immutable registration"
        ) from error

    except (BotoCoreError, ClientError) as error:
        raise ConfigurationError(
            "AWS deployment finalization failed; retry with the same "
            "deployment outputs after resolving the failure"
        ) from error