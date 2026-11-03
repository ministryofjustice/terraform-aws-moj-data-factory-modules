import base64
import copy
import re

from ingestion_config.inventory import validate_inventory
from ingestion_config.loader import ConfigurationError


REPOSITORY = "ministryofjustice/terraform-aws-moj-data-factory-modules"
HANDLER = "dms_runtime.lambda_function.lambda_handler"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigurationError(message)


def artifacts_from(inventory: dict) -> dict:
    require(
        isinstance(inventory, dict)
        and set(inventory) == {"compiled_artifacts"}
        and isinstance(inventory["compiled_artifacts"], dict),
        "Inventory must contain a compiled_artifacts object",
    )

    artifacts = inventory["compiled_artifacts"]

    validate_inventory(artifacts)

    return artifacts


def build_deployment_inputs(
    *,
    inventory: dict,
    foundations: dict,
    runtime_package: dict,
    capacity_assignments: dict,
    retained_inventory: dict,
    retained_assignments: dict,
    compiler_commit: str,
) -> dict:
    try:
        artifacts = artifacts_from(inventory)
        retained = artifacts_from(retained_inventory)

        require(
            bool(artifacts),
            "Deployment inventory must not be empty",
        )

        require(
            re.fullmatch(r"[a-f0-9]{40}", compiler_commit) is not None,
            "Compiler commit must be a full Git commit SHA",
        )

        require(
            foundations["environment"] == "development"
            and foundations["region"] == "eu-west-2",
            "Deployment is restricted to development in eu-west-2",
        )

        account = foundations["account_id"]

        require(
            isinstance(account, str)
            and re.fullmatch(r"[0-9]{12}", account) is not None,
            "Platform account ID must contain 12 digits",
        )

        prefix = f"arn:aws:{{service}}:eu-west-2:{account}:"

        registry = foundations["registry"]

        require(
            set(registry) == {"table_name", "table_arn"},
            "Unexpected Registry reference fields",
        )

        require(
            registry["table_arn"]
            == (
                prefix.format(service="dynamodb")
                + "table/"
                + registry["table_name"]
            ),
            "Registry table must belong to the deployment account and region",
        )

        documents = foundations["published_documents"]

        require(
            set(documents)
            == {"bucket_name", "bucket_owner", "kms_key_arns"},
            "Unexpected configuration storage fields",
        )

        require(
            documents["bucket_owner"] == account
            and bool(documents["bucket_name"])
            and documents["bucket_name"]
            == documents["bucket_name"].strip()
            and bool(documents["kms_key_arns"])
            and all(
                key.startswith(
                    prefix.format(service="kms") + "key/"
                )
                for key in documents["kms_key_arns"]
            ),
            "Configuration storage must identify its bucket owner "
            "and encryption keys",
        )

        package = runtime_package

        require(
            package["source_repository"] == REPOSITORY
            and re.fullmatch(
                r"[a-f0-9]{40}",
                package["source_commit"],
            ) is not None
            and package["region"] == "eu-west-2"
            and package["python_version"] == "3.12"
            and package["architecture"] == "x86_64"
            and package["handler"] == HANDLER,
            "Runtime package provenance or runtime settings do not match",
        )

        digest = package["sha256"]

        require(
            re.fullmatch(r"[a-f0-9]{64}", digest) is not None,
            "Runtime package SHA-256 must be lowercase hexadecimal",
        )

        require(
            package["source_code_hash"]
            == base64.b64encode(
                bytes.fromhex(digest)
            ).decode("ascii"),
            "Runtime package checksums disagree",
        )

        require(
            package["object_key"]
            == (
                "lambda/dms-runtime-controller/"
                f"{package['source_commit']}/{digest}/package.zip"
            )
            and bool(package["version_id"])
            and package["version_id"] != "null"
            and package["version_id"]
            == package["version_id"].strip()
            and bool(package["bucket_name"])
            and package["bucket_name"]
            == package["bucket_name"].strip()
            and package["kms_key_arn"].startswith(
                prefix.format(service="kms") + "key/"
            ),
            "Runtime package must identify an exact versioned "
            "deployment object",
        )

        require(
            isinstance(capacity_assignments, dict)
            and set(capacity_assignments) == set(artifacts),
            "Capacity assignments must exactly match "
            "the deployment inventory",
        )

        require(
            isinstance(retained_assignments, dict)
            and set(retained_assignments) == set(retained),
            "Retained assignments must exactly match "
            "the retained inventory",
        )

        for key, artifact in retained.items():
            require(
                key in artifacts and artifacts[key] == artifact,
                "A retained generation was removed or changed",
            )

            require(
                capacity_assignments[key] == retained_assignments[key],
                "A retained generation's capacity assignment was changed",
            )

        pools = foundations["replication_capacities"]

        for pool in pools.values():
            require(
                set(pool) == {
                    "vpc_id",
                    "replication_instance_arn",
                    "replication_instance_id",
                    "replication_security_group_id",
                    "replication_security_group_ids",
                    "replication_subnet_group_id",
                    "task_log_group",
                },
                "Unexpected or missing capacity binding fields",
            )

            if pool["task_log_group"] is not None:
                require(
                    set(pool["task_log_group"]) == {"arn", "name"},
                    "Unexpected task log group fields",
                )

        for key, artifact in artifacts.items():
            assignments = capacity_assignments[key]

            require(
                isinstance(assignments, dict)
                and set(assignments) == set(artifact["module_inputs"]),
                "Every task group requires an explicit capacity assignment",
            )

            for group, pool_name in assignments.items():
                require(
                    pool_name in pools,
                    "An assigned capacity pool does not exist",
                )

                pool = pools[pool_name]
                inputs = artifact["module_inputs"][group]

                require(
                    pool["replication_instance_arn"].startswith(
                        prefix.format(service="dms") + "rep:"
                    ),
                    "Capacity must belong to the deployment "
                    "account and region",
                )

                require(
                    pool["vpc_id"] == inputs["vpc_id"]
                    and set(
                        inputs["network"][
                            "additional_security_group_ids"
                        ]
                    )
                    <= set(pool["replication_security_group_ids"])
                    and pool.get("task_log_group") is not None,
                    "Assigned capacity must provide the required VPC, "
                    "source access and logs",
                )

        services = {
            artifact["runtime_configuration"]["source"]
            for artifact in artifacts.values()
        }

        require(
            all(
                re.fullmatch(r"[a-z][a-z0-9-]{0,29}", name)
                for name in services
            ),
            "Service names must be valid runtime identifiers",
        )

        credentials = foundations["source_credentials"]

        require(
            set(credentials) == services,
            "Source preparation settings must exactly match "
            "the retained services",
        )

        runtime_settings = {}

        for service in sorted(services):
            preparation = credentials[service]
            mode = preparation["mode"]
            sync = preparation.get("sync_function_arn")

            require(
                set(preparation) <= {
                    "mode",
                    "sync_function_arn",
                },
                "Source preparation settings must not contain "
                "credential values",
            )

            require(
                (
                    mode == "connection_test"
                    and sync is None
                )
                or (
                    mode == "synchronise_then_test"
                    and isinstance(sync, str)
                    and re.fullmatch(
                        prefix.format(service="lambda")
                        + r"function:[A-Za-z0-9_-]+"
                        + r"(?::[A-Za-z0-9_-]+)?",
                        sync,
                    ) is not None
                ),
                "Source preparation mode and synchronisation "
                "function do not match",
            )

            runtime_settings[service] = {
                "published_documents": {
                    **documents,
                    "prefix": f"services/{service}/",
                },
                "source_credentials": preparation,
            }

        result = {
            "deployment": {
                "environment": "development",
                "account_id": account,
                "region": "eu-west-2",
                "compiler_commit": compiler_commit,
                "compiled_artifacts": artifacts,
                "artifact": {
                    name: package[name]
                    for name in (
                        "bucket_name",
                        "object_key",
                        "version_id",
                        "source_code_hash",
                    )
                },
                "registry": registry,
                "runtime_settings": runtime_settings,
                "replication_capacities": pools,
                "capacity_assignments": capacity_assignments,
            }
        }

        return copy.deepcopy(result)

    except ConfigurationError:
        raise

    except (
        KeyError,
        TypeError,
        AttributeError,
        ValueError,
    ) as error:
        raise ConfigurationError(
            "Incomplete or invalid deployment preparation inputs"
        ) from error