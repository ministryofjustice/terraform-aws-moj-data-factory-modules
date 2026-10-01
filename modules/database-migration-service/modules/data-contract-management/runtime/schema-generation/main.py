import json
import os
import uuid

import boto3
from sqlalchemy import create_engine, inspect
from Logger import get_logger


logger = get_logger(__name__)


# Lambda environment configuration
db_secret_arn = os.getenv("DB_SECRET_ARN")
schema_registry_bucket = os.getenv("SCHEMA_REGISTRY_BUCKET")

namespace = os.getenv("CONTRACT_NAMESPACE", "")
service = os.getenv("CONTRACT_SERVICE")


# AWS clients
def _get_secretmanager():
    return boto3.client("secretsmanager")


def _get_s3():
    return boto3.client("s3")


def get_database_secret():
    # Retrieve database connection details from Secrets Manager.
    secretsmanager = _get_secretmanager()

    db_secret_response = secretsmanager.get_secret_value(
        SecretId=db_secret_arn
    )

    return json.loads(db_secret_response["SecretString"])


def convert_postgres_type(column_type):
    # Convert PostgreSQL column types to AVRO-compatible types.
    type_name = str(column_type).lower()

    if "uuid" in type_name:
        return {
            "type": "string",
            "logicalType": "UUID"
        }
    elif "bigint" in type_name:
        return "long"
    elif "smallint" in type_name or "integer" in type_name:
        return "int"
    elif "boolean" in type_name:
        return "boolean"
    elif "timestamp" in type_name:
        return {
            "type": "long",
            "logicalType": "timestamp-micros"
        }
    elif "date" in type_name:
        return {
            "type": "int",
            "logicalType": "date"
        }
    elif "double" in type_name:
        return "double"
    elif "real" in type_name or "float" in type_name:
        return "float"
    elif "numeric" in type_name or "decimal" in type_name:
        # TODO: Confirm whether decimal precision/scale
        # should be preserved in the contract.
        return "double"
    elif "char" in type_name or "text" in type_name:
        return "string"
    else:
        raise ValueError(
            f"Unsupported PostgreSQL type: {column_type}"
        )


def convert_oracle_type(column_type):
    # Convert Oracle column types to AVRO-compatible types.
    type_name = str(column_type).lower()

    if "number" in type_name:
        # TODO: Confirm required handling of Oracle
        # NUMBER precision and scale.
        return "double"
    elif "integer" in type_name:
        return "int"
    elif "float" in type_name:
        return "float"
    elif "timestamp" in type_name:
        return {
            "type": "long",
            "logicalType": "timestamp-micros"
        }
    elif "date" in type_name:
        return {
            "type": "long",
            "logicalType": "timestamp-micros"
        }
    elif "char" in type_name or "clob" in type_name:
        return "string"
    elif "blob" in type_name or "raw" in type_name:
        return "bytes"
    else:
        raise ValueError(
            f"Unsupported Oracle type: {column_type}"
        )


def get_contract_versions(version_number):
    """
    Return both agreed representations of the contract version.

    Example:
        1 -> ("v1", "1.0.0")
        2 -> ("v2", "2.0.0")
    """
    return (
        f"v{version_number}",
        f"{version_number}.0.0"
    )


def build_avro_contract(
    source,
    namespace,
    service,
    table_name,
    schema_name,
    database_name,
    database_type,
    contract_version,
    version,
    avro_fields,
):
    # Build the generated data contract.
    return {
        "type": "record",
        "namespace": namespace,
        "service": service,
        "version": version,
        "contract_version": contract_version,
        "contract_id": str(uuid.uuid4()),
        "name": table_name,
        "source": source,
        "database_name": database_name,
        "schema_name": schema_name,
        "table_name": table_name,
        "description": "",
        "contacts": {
            "team": (
                "https://dsdmoj.atlassian.net/wiki/"
                "spaces/DPR/overview?homepageId=4015489077"
            ),
            "slack": "#ask_dpr"
        },
        "database": {
            "type": database_type,
            "name": database_name
        },
        "fields": avro_fields
    }


def validate_configuration():
    # Validate required Lambda environment configuration.
    required_config = {
        "DB_SECRET_ARN": db_secret_arn,
        "SCHEMA_REGISTRY_BUCKET": schema_registry_bucket,
        "ENGINE": os.getenv("ENGINE"),
        "DATABASE_NAME": os.getenv("DATABASE_NAME"),
        "SCHEMA_NAME": os.getenv("SCHEMA_NAME"),
        "TABLE_LIST": os.getenv("TABLE_LIST"),
    }

    missing_config = [
        name
        for name, value in required_config.items()
        if not value
    ]

    if os.getenv("ENGINE", "").lower() == "oracle":
        if not os.getenv("DATABASE_SID"):
            missing_config.append("DATABASE_SID")

    if missing_config:
        raise ValueError(
            "Missing required environment configuration: "
            + ", ".join(missing_config)
        )


def validate_contract(contract):
    # Perform basic validation of the generated contract.
    required_fields = [
        "type",
        "name",
        "version",
        "contract_version",
        "contract_id",
        "source",
        "database_name",
        "schema_name",
        "table_name",
        "database",
        "fields",
        "service",
    ]

    for field in required_fields:
        if field not in contract:
            raise ValueError(
                f"Contract is missing required field: {field}"
            )

    if contract["type"] != "record":
        raise ValueError(
            "Contract type must be 'record'"
        )

    if not contract["fields"]:
        raise ValueError(
            "Contract must contain at least one field"
        )

    for avro_field in contract["fields"]:
        if "name" not in avro_field:
            raise ValueError(
                "Contract field is missing required field: name"
            )

        if "type" not in avro_field:
            raise ValueError(
                "Contract field is missing required field: type"
            )

    return True


def store_contract(contract_json, s3_key):
    # Store the generated contract in the S3 schema registry.
    s3 = _get_s3()

    s3.put_object(
        Bucket=schema_registry_bucket,
        Key=s3_key,
        Body=contract_json,
        ContentType="application/json"
    )


def build_database_connection(
    engine_type,
    db_name,
    db_secret,
):
    username = db_secret["username"]
    password = db_secret["password"]
    host = db_secret["host"]

    if engine_type == "oracle":
        port = db_secret.get("port", 1521)
        oracle_service_name = os.getenv("DATABASE_SID")

        if not oracle_service_name:
            raise ValueError(
                "DATABASE_SID is required for Oracle"
            )

        return (
            f"oracle+oracledb://{username}:{password}"
            f"@{host}:{port}/"
            f"?service_name={oracle_service_name}"
        )

    if engine_type == "postgres":
        port = db_secret.get("port", 5432)

        return (
            f"postgresql://{username}:{password}"
            f"@{host}:{port}/{db_name}"
        )

    raise ValueError(
        f"Unsupported database engine: {engine_type}"
    )


def generate_contract_for_table(
    inspector,
    engine_type,
    db_name,
    schema_name,
    table_name,
    execution_id,
):
    logger.info(
        "Schema generation started",
        extra={
            "execution_id": execution_id,
            "database_name": db_name,
            "schema_name": schema_name,
            "table_name": table_name,
        }
    )

    columns = inspector.get_columns(
        table_name,
        schema=schema_name
    )

    primary_key = inspector.get_pk_constraint(
        table_name,
        schema=schema_name
    )

    primary_key_columns = primary_key.get(
        "constrained_columns",
        []
    )

    logger.info(
        "Source metadata retrieved",
        extra={
            "execution_id": execution_id,
            "database_name": db_name,
            "schema_name": schema_name,
            "table_name": table_name,
        }
    )

    avro_fields = []

    for column in columns:
        if engine_type == "oracle":
            avro_type = convert_oracle_type(
                column["type"]
            )
        else:
            avro_type = convert_postgres_type(
                column["type"]
            )

        field = {
            "name": column["name"],
            "type": avro_type,
            "nullable": column["nullable"]
        }

        if column["name"] in primary_key_columns:
            field["key"] = "primary"

        avro_fields.append(field)

    # Current agreed initial version.
    # Future version creation can change the number supplied here
    # without changing the version formatting itself.
    version_number = 1

    contract_version, version = get_contract_versions(
        version_number
    )

    contract = build_avro_contract(
        source=engine_type,
        namespace=namespace,
        service=service,
        table_name=table_name,
        schema_name=schema_name,
        database_name=db_name,
        database_type=engine_type,
        contract_version=contract_version,
        version=version,
        avro_fields=avro_fields,
    )

    validate_contract(contract)

    contract_json = json.dumps(
        contract,
        indent=2
    )

    # Schema registry structure:
    # source/database/schema/table/version/contract.avsc
    s3_key = (
        f"{engine_type}/{db_name}/{schema_name}/"
        f"{table_name}/{contract_version}/contract.avsc"
    )

    store_contract(
        contract_json,
        s3_key
    )

    logger.info(
        "Schema generated and stored successfully",
        extra={
            "execution_id": execution_id,
            "database_name": db_name,
            "schema_name": schema_name,
            "table_name": table_name,
            "object_key": s3_key,
        }
    )

    return {
        "table_name": table_name,
        "s3_key": s3_key,
        "contract_id": contract["contract_id"],
        "contract_version": contract_version,
        "version": version,
    }


def lambda_handler(event, context):
    validate_configuration()

    execution_id = context.aws_request_id

    engine_type = os.getenv("ENGINE").lower()
    db_name = os.getenv("DATABASE_NAME")
    schema_name = os.getenv("SCHEMA_NAME")

    # TABLE_LIST is supplied as a comma-separated environment value.
    table_list = [
        table.strip()
        for table in os.getenv("TABLE_LIST").split(",")
        if table.strip()
    ]

    if not table_list:
        raise ValueError(
            "TABLE_LIST must contain at least one table"
        )

    if engine_type not in ("postgres", "oracle"):
        raise ValueError(
            f"Unsupported database engine: {engine_type}"
        )

    db_secret = get_database_secret()

    db_string = build_database_connection(
        engine_type,
        db_name,
        db_secret,
    )

    engine = create_engine(db_string)
    inspector = inspect(engine)

    generated_contracts = []

    for table_name in table_list:
        result = generate_contract_for_table(
            inspector=inspector,
            engine_type=engine_type,
            db_name=db_name,
            schema_name=schema_name,
            table_name=table_name,
            execution_id=execution_id,
        )

        generated_contracts.append(result)

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": (
                "Schema generation completed successfully"
            ),
            "contracts": generated_contracts,
        })
    }
