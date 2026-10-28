from ingestion_config.compiler import fingerprint
from ingestion_config.deployment_inventory import validate_deployment_inventory
from ingestion_config.loader import ConfigurationError
from ingestion_config.manifest_builder import build_deployment_manifest


def build_deployment_bundle(deployments: dict, *, account_id: str) -> dict:
    if not isinstance(deployments, dict) or not deployments:
        raise ConfigurationError(
            "Deployment output must contain at least one service"
        )

    services = {}
    manifests = []

    for name, deployment in sorted(deployments.items()):
        try:
            artifact = deployment["compiled_artifact"]
            runtime = artifact["runtime_configuration"]

            valid_keys = (
                runtime["source"],
                (
                    f"{runtime['source']}/"
                    f"{artifact['extraction_generation']}"
                ),
            )

            if name not in valid_keys:
                raise ConfigurationError(
                    "Deployment key must match its service "
                    "and extraction generation"
                )

            if (
                runtime["environment"] != "development"
                or runtime["region"] != "eu-west-2"
            ):
                raise ConfigurationError(
                    "Publication is restricted to development in eu-west-2"
                )

            original = {
                key: value
                for key, value in artifact.items()
                if key != "artifact_revision"
            }

            if fingerprint(original) != artifact["artifact_revision"]:
                raise ConfigurationError(
                    "Compiler artifact revision does not match its contents"
                )

            manifest = build_deployment_manifest(
                artifact,
                deployment["deployment_bindings"],
                account_id=account_id,
            )

            services[name] = {
                "artifact": artifact,
                "manifest": manifest,
            }
            manifests.append(manifest)

        except (KeyError, TypeError, AttributeError) as error:
            raise ConfigurationError(
                "Incomplete service deployment output"
            ) from error

    validate_deployment_inventory(
        manifests,
        environment="development",
        account_id=account_id,
        region="eu-west-2",
    )

    return {
        "bundle_version": 1,
        "environment": "development",
        "account_id": account_id,
        "region": "eu-west-2",
        "services": services,
    }