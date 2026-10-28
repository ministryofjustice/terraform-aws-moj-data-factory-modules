import copy
from pathlib import Path

from ingestion_config.compiler import compile_service, fingerprint
from ingestion_config.loader import ConfigurationError


def deployment_key(artifact: dict) -> str:
    runtime = artifact["runtime_configuration"]

    return (
        f"{runtime['source']}/"
        f"{artifact['extraction_generation']}"
    )


def validate_inventory(artifacts: dict) -> None:
    deployments = set()
    boundaries = []
    services = {}

    for key, artifact in artifacts.items():
        try:
            runtime = artifact["runtime_configuration"]

            if key != deployment_key(artifact):
                raise ConfigurationError(
                    "Deployment key must be source/extraction-generation"
                )

            if (
                artifact["artifact_version"] != 1
                or runtime["environment"] != "development"
                or runtime["region"] != "eu-west-2"
                or runtime["extraction_generation"]
                != artifact["extraction_generation"]
            ):
                raise ConfigurationError(
                    "Inventory requires version 1 development "
                    "artifacts in eu-west-2"
                )

            content = {
                name: value
                for name, value in artifact.items()
                if name != "artifact_revision"
            }

            if fingerprint(content) != artifact["artifact_revision"]:
                raise ConfigurationError(
                    "Retained artifact revision does not match its contents"
                )

            if (
                not artifact["module_inputs"]
                or set(artifact["module_inputs"])
                != set(runtime["task_groups"])
            ):
                raise ConfigurationError(
                    "Artifact task groups must match their module inputs"
                )

            source = runtime["source"]

            identity = (
                runtime["source_endpoint"]["engine"],
                runtime["source_endpoint"]["database_name"],
            )

            if source in services and services[source] != identity:
                raise ConfigurationError(
                    "Retained service engine and database identity "
                    "must not change"
                )

            services[source] = identity

            for inputs in artifact["module_inputs"].values():
                name = inputs["name"]

                if name in deployments:
                    raise ConfigurationError(
                        "Deployment names collide across generations"
                    )

                deployments.add(name)

                boundary = (
                    inputs["target"]["bucket_name"],
                    inputs["target"]["bucket_folder"] + "/",
                )

                if any(
                    bucket == boundary[0]
                    and (
                        prefix.startswith(boundary[1])
                        or boundary[1].startswith(prefix)
                    )
                    for bucket, prefix in boundaries
                ):
                    raise ConfigurationError(
                        "Landing locations overlap across generations"
                    )

                boundaries.append(boundary)

        except (KeyError, TypeError, AttributeError) as error:
            raise ConfigurationError(
                "Incomplete compiled artifact inventory"
            ) from error


def compile_inventory(
    directory: Path,
    platform: Path,
    generations: dict,
    *,
    retained: dict | None = None,
) -> dict:
    if not directory.is_dir():
        raise ConfigurationError(
            "Service configuration directory does not exist"
        )

    paths = sorted([
        *directory.glob("*.yaml"),
        *directory.glob("*.yml"),
    ])

    if not paths:
        raise ConfigurationError("No service YAML files found")

    names = [path.stem for path in paths]

    if len(names) != len(set(names)):
        raise ConfigurationError("Duplicate service filenames")

    if set(names) != set(generations):
        raise ConfigurationError(
            "Generation keys must exactly match service filenames"
        )

    if retained is not None and (
        set(retained) != {"compiled_artifacts"}
        or not isinstance(retained["compiled_artifacts"], dict)
    ):
        raise ConfigurationError(
            "Retained inventory must contain a compiled_artifacts object"
        )

    artifacts = copy.deepcopy(
        retained["compiled_artifacts"]
        if retained is not None
        else {}
    )

    validate_inventory(artifacts)

    for path in paths:
        if path.is_symlink():
            raise ConfigurationError(
                "Service YAML must not be a symbolic link"
            )

        artifact = compile_service(
            path,
            platform,
            extraction_generation=generations[path.stem],
        )

        if artifact["runtime_configuration"]["source"] != path.stem:
            raise ConfigurationError(
                "Service filename must match the configured service name"
            )

        key = deployment_key(artifact)

        if key in artifacts and artifacts[key] != artifact:
            raise ConfigurationError(
                "Prepared generation differs; "
                "use a fresh extraction generation"
            )

        artifacts[key] = artifact

    validate_inventory(artifacts)

    return {
        "compiled_artifacts": dict(sorted(artifacts.items()))
    }