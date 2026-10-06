import json
import os
import re

import boto3
from sqlalchemy import create_engine, inspect
from fastavro.schema import parse_schema
from botocore.exceptions import ClientError
from urllib.parse import quote_plus
from Logger import get_logger


logger = get_logger("schema-generation")

s3 = boto3.client("s3")

SUPPORTED_ENGINES = {
"postgres",
"oracle",
}

def validate_configuration():
    """
    Validate Lambda environment configuration.

    Raises:
        ValueError: If any required configuration is missing or invalid.

    Returns:
        dict: Validated configuration values.
    """

    config = {
        "DB_SECRET_ARN": os.getenv("DB_SECRET_ARN"),
        "SCHEMA_REGISTRY_BUCKET": os.getenv("SCHEMA_REGISTRY_BUCKET"),
        "ENGINE": os.getenv("ENGINE"),
        "NAMESPACE": os.getenv("NAMESPACE"),
        "SERVICE": os.getenv("SERVICE"),
        "DATABASE_NAME": os.getenv("DATABASE_NAME"),
        "SCHEMA_NAME": os.getenv("SCHEMA_NAME"),
        "CONTRACT_VERSION": os.getenv("CONTRACT_VERSION"),
        "VERSION": os.getenv("VERSION"),
        "TABLE_LIST": os.getenv("TABLE_LIST")
    }

    # Validate required values exist and are not empty.
    missing_config = [
        name
        for name, value in config.items()
        if value is None or not str(value).strip()
    ]

    if missing_config:
        raise ValueError(
            f"Missing required environment variables: "
            f"{', '.join(sorted(missing_config))}"
        )

    # Validate engine.
    engine = config["ENGINE"].strip().lower()

    if engine not in SUPPORTED_ENGINES:
        raise ValueError(
            f"Unsupported ENGINE '{config['ENGINE']}'. "
            f"Supported values are: "
            f"{', '.join(sorted(SUPPORTED_ENGINES))}"
        )

    if not config["DB_SECRET_ARN"].startswith(
    "arn:aws:secretsmanager:"
    ):
        raise ValueError("DB_SECRET_ARN must be a valid AWS Secrets Manager ARN")

    # Validate identifiers.
    identifier_fields = [
        "NAMESPACE",
        "SERVICE",
        "DATABASE_NAME",
        "SCHEMA_NAME",
    ]

    for field in identifier_fields:
        value = config[field].strip()

        if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError(
                f"{field} contains invalid characters. "
                f"Allowed: letters, numbers, underscore, hyphen."
            )

    # Parse and validate table list.
    table_list = [
        table.strip()
        for table in config["TABLE_LIST"].split(",")
        if table.strip()
    ]

    if not table_list:
        raise ValueError(
            "TABLE_LIST must contain at least one table."
        )

    invalid_tables = [
        table
        for table in table_list
        if not re.fullmatch(r"[A-Za-z0-9_-]+", table)
    ]

    if invalid_tables:
        raise ValueError(
            f"Invalid table names in TABLE_LIST: "
            f"{', '.join(invalid_tables)}"
        )

    config["TABLE_LIST"] = table_list

    return config



def get_database_secret(db_secret_arn):
    """
    Retrieve and parse the database credentials stored in AWS Secrets Manager.

    Returns:
        dict: Secret contents.

    Raises:
        ValueError: Secret value is missing or invalid.
        ClientError: AWS Secrets Manager request failed.
    """
    secretsmanager = boto3.client("secretsmanager")
    try:
        response = secretsmanager.get_secret_value(
            SecretId=db_secret_arn
        )

        secret_string = response.get("SecretString")

        if not secret_string:
            raise ValueError(
                f"Secret '{db_secret_arn}' does not contain a SecretString."
            )

        return json.loads(secret_string)

    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Secret '{db_secret_arn}' contains invalid JSON."
        ) from exc

    except ClientError:
        logger.exception(
            "Failed to retrieve secret '%s'.",
            db_secret_arn,
        )
        raise

def build_database_connection(
    engine: str,
    database_name: str,
    db_secret: dict,
) -> str:
    required_fields = {
        "username",
        "password",
        "host",
    }

    missing_fields = [
        field
        for field in required_fields
        if not db_secret.get(field)
    ]

    if missing_fields:
        raise ValueError(
            "Database secret missing required fields: "
            f"{', '.join(sorted(missing_fields))}"
        )

    username = quote_plus(str(db_secret["username"]))
    password = quote_plus(str(db_secret["password"]))
    host = db_secret["host"]

    if engine == "oracle":
        port = int(db_secret.get("port", 1521))
        oracle_service_name = os.getenv("DATABASE_SID")
        if not  oracle_service_name :
            raise ValueError(
                "DATABASE_SID is required for Oracle."
            )

        return (
            f"oracle+oracledb://{username}:{password}"
            f"@{host}:{port}"
            f"/?service_name={oracle_service_name}"
        )

    if engine == "postgres":
        if not database_name:
            raise ValueError(
                "DATABASE_NAME is required for PostgreSQL."
            )

        port = int(db_secret.get("port", 5432))

        return (
            f"postgresql://{username}:{password}"
            f"@{host}:{port}/{database_name}"
        )

    # Defensive check.
    raise ValueError(
        f"Unsupported database engine: {engine}"
    )


def create_database_engine(
    connection_url: str,
    engine: str,
    database_name: str,
):
    try:
        logger.info(
            "Creating SQLAlchemy engine",
            extra={
                "engine": engine,
                "database_name": database_name,
            },
        )

        return create_engine(
            connection_url,
            pool_pre_ping=True,
            pool_recycle=300,
        )

    except Exception:
        logger.exception(
            "Failed to create SQLAlchemy engine",
            extra={
                "engine": engine,
                "database_name": database_name,
            },
        )
        raise


def convert_postgres_type(column_type):
    # Convert PostgreSQL column types to AVRO-compatible types.
    type_name = str(column_type).lower()

    if "uuid" in type_name:
        return {
            "type": "string",
            "logicalType": "uuid"
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
        precision = getattr(column_type, "precision", None)
        scale = getattr(column_type, "scale", 0)

        if precision is None:
            raise ValueError(
                f"Precision is required for decimal type: {column_type}"
            )

        return {
            "type": "bytes",
            "logicalType": "decimal",
            "precision": precision,
            "scale": scale or 0,
        }
    elif "char" in type_name or "text" in type_name:
        return "string"
    else:
        raise ValueError(
            f"Unsupported PostgreSQL type: {column_type}"
        )



def convert_oracle_type(column_type):
    """
    Convert Oracle column types to Avro-compatible types.

    Args:
        column_type: SQLAlchemy Oracle column type.

    Returns:
        Avro type definition.

    Raises:
        ValueError: Unsupported Oracle type.
    """

    type_name = str(column_type).lower()

    if "number" in type_name:
        precision = getattr(
            column_type,
            "precision",
            None,
        )

        scale = getattr(
            column_type,
            "scale",
            None,
        )

        # NUMBER with no precision defined
        if precision is None:
            return "double"

        # Integer NUMBER types
        if scale in (None, 0):
            if precision < = 9:
                return "int"

            return "long"

        # Decimal NUMBER types
        return {
            "type": "bytes",
            "logicalType": "decimal",
            "precision": precision,
            "scale": scale,
        }

    if "integer" in type_name:
        return "int"

    if "binary_float" in type_name:
        return "float"

    if "binary_double" in type_name:
        return "double"

    if "float" in type_name:
        return "double"

    if "timestamp" in type_name:
        return {
            "type": "long",
            "logicalType": "timestamp-micros",
        }

    if "date" in type_name:
        return {
            "type": "long",
            "logicalType": "timestamp-micros",
        }

    if any(
        value in type_name
        for value in (
            "varchar",
            "varchar2",
            "nvarchar2",
            "char",
            "nchar",
            "clob",
            "nclob",
            "long",
        )
    ):
        return "string"

    if any(
        value in type_name
        for value in (
            "blob",
            "raw",
            "long raw",
        )
    ):
        return "bytes"

    raise ValueError(
        f"Unsupported Oracle type "
        f"'{column_type}' ({type_name})"
    )

def build_avro_contract(
    namespace,
    service,
    table_name,
    schema_name,
    database_name,
    engine_type,
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
        "name": table_name,
        "database_name": database_name,
        "schema_name": schema_name,
        "table_name": table_name,
        "description": "",
        "contacts": {
            "team": "https://dsdmoj.atlassian.net/wiki/spaces/DPR/overview?homepageId=4015489077",
            "slack": "#ask_dpr",
        },
        "database": {
            "type": engine_type,
            "name": database_name
        },
        "fields": avro_fields
    }




# Read table metadata
def get_table_metadata(inspector,schema_name: str,table_name: str,)-> tuple[list, list]:
    """
    Retrieve column and primary key metadata.
    """

    if table_name not in inspector.get_table_names(schema=schema_name):
        raise ValueError(
            f"Table '{schema_name}.{table_name}' does not exist"
        )

    columns = inspector.get_columns(
        table_name,
        schema=schema_name,
    )

    primary_key = inspector.get_pk_constraint(
        table_name,
        schema=schema_name,
    )

    primary_key_columns = primary_key.get(
        "constrained_columns",
        [],
    )

    if not primary_key_columns:
        logger.warning(
            "Table %s has no primary key",
            table_name
        )

    return columns, primary_key_columns

#Convert database columns to Avro fields
def build_avro_fields(
    columns: list,
    primary_key_columns: list,
    engine_type: str,
)-> list:
    """
    Convert database columns to Avro field definitions.
    """

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

        if column["nullable"]:
             avro_type = ["null", avro_type]

        field = {
            "name": column["name"],
            "type": avro_type,
        }

        if column["name"] in primary_key_columns:
            field["key"] = "primary"

        avro_fields.append(field)

    return avro_fields



def validate_contract(contract: dict,)-> bool:
    """
    Validate generated contract structure.
    """

    required_fields = [
        "type",
        "name",
        "version",
        "namespace",
        "contract_version",
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
                f"Contract is missing required field '{field}'."
            )

    if contract["type"] != "record":
        raise ValueError(
            "Contract type must be 'record'."
        )

    fields = contract.get("fields", [])

    if not fields:
        raise ValueError(
            "Contract must contain at least one field."
        )

    for field in fields:

        if not field.get("name"):
            raise ValueError(
                "Contract field is missing required field 'name'."
            )

        if "type" not in field:
            raise ValueError(
                f"Contract field '{field.get('name', 'unknown')}' "
                "is missing required field 'type'."
            )

    return True

def validate_avro_contract(contract: dict,)-> bool:
    """
    Validate contract structure and Avro schema.

    Returns:
        bool: True if validation succeeds, False otherwise.
    """

    try:
        if validate_contract(contract):

            avro_schema = {
                "type": contract["type"],
                "name": contract["name"],
                "namespace": contract["namespace"],
                "fields": contract["fields"],
}
            parse_schema(avro_schema)

            logger.info(
                "Contract and Avro schema validation successful"
            )

            return True

        return False

    except Exception:
        logger.exception(
            "Contract and Avro schema validation failed"
        )
        return False


def save_contract(
    schema_registry_bucket: str,
    contract: dict,
    engine_type: str,
    database_name: str,
    schema_name: str,
    table_name: str,
    contract_version: str,
)-> str:
    """
    Store contract in schema registry.

    Returns:
        str: S3 object key.
    """
   
    object_key = (
        f"{engine_type}/"
        f"{database_name}/"
        f"{schema_name}/"
        f"{table_name}/"
        f"{contract_version}/"
        "contract.avsc"
    )

    # Verify contract does not already exist
    try:
        s3.head_object(
            Bucket=schema_registry_bucket,
            Key=object_key,
        )

        raise ValueError(
            f"Schema already exists: "
            f"s3://{schema_registry_bucket}/{object_key}"
        )

    except ClientError as e:
        error_code = e.response["Error"]["Code"]

        # Expected when object does not exist
        if error_code not in (
            "404",
            "NoSuchKey",
            "NotFound",
        ):
            logger.exception(
                "Failed checking schema existence"
            )
            raise

    # Upload contract
    try:
        s3.put_object(
            Bucket=schema_registry_bucket,
            Key=object_key,
            Body=json.dumps(
                contract,
                indent=2,
            ).encode("utf-8"),
            ContentType="application/avro+json",
        )

        logger.info(
            "Contract stored successfully",
            extra={
                "bucket": schema_registry_bucket,
                "object_key": object_key,
            },
        )

        return object_key

    except ClientError:
        logger.exception(
            "Failed to store contract in S3",
            extra={
                "bucket": schema_registry_bucket,
                "object_key": object_key,
            },
        )
        raise


def generate_contract(
    inspector,
    engine_type: str,
    database_name: str,
    schema_name: str,
    table_name: str,
    service: str,
    schema_registry_bucket: str,
    audit_table: str,
    contract_version: str,
    version: str,
    namespace: str,
    execution_id: str,
):
    """
    Generate and store schema contract for a table.
    """

    logger.info(
        "Schema generation started",
        extra={
            "execution_id": execution_id,
            "database_name": database_name,
            "schema_name": schema_name,
            "table_name": table_name,
        },
    )

    columns, primary_key_columns = get_table_metadata(inspector,schema_name,table_name,
    )

    logger.info(
        "Source metadata retrieved",
        extra={
            "execution_id": execution_id,
            "database_name": database_name,
            "schema_name": schema_name,
            "table_name": table_name,
            "column_count": len(columns),
        },
    )

    avro_fields = build_avro_fields(
        columns,
        primary_key_columns,
        engine_type,
    )

    contract = build_avro_contract(namespace,service,table_name,schema_name,database_name,engine_type,contract_version,version,avro_fields)

    if not validate_avro_contract(contract):
        raise ValueError(
            f"Contract validation failed for table '{table_name}'."
    )

    s3_key = save_contract(schema_registry_bucket,contract,engine_type,database_name,schema_name,table_name,contract_version)

    logger.info(
        "Schema generated and stored successfully",
        extra={
            "execution_id": execution_id,
            "database_name": database_name,
            "schema_name": schema_name,
            "table_name": table_name,
            "contract_version": contract_version,
            "object_key": s3_key,
        },
    )

    return {
        "table_name": table_name,
        "contract_version": contract_version,
        "version": version,
        "s3_key": s3_key,
    }

def lambda_handler(event, context):

    execution_id = context.aws_request_id

    #validate lambda configuration
    config = validate_configuration()

    engine_type = config["ENGINE"]
    service = config["SERVICE"]
    database_name = config["DATABASE_NAME"]
    schema_name = config["SCHEMA_NAME"]
    namespace = config["NAMESPACE"]
    db_secret = config["DB_SECRET_ARN"]
    schema_registry = config["SCHEMA_REGISTRY_BUCKET"]
    audit_table = config["AUDIT_TABLE_NAME"]
    contract_version = config["CONTRACT_VERSION"]
    version = config["VERSION"]
    table_list = config["TABLE_LIST"]

    #Read db config form secret manager
    db_secret = get_database_secret(db_secret)

   
    #Build db connection
    connection_url = build_database_connection(engine_type,database_name,db_secret)

    #create engine
    db_engine = create_database_engine(connection_url,engine_type, database_name)

     try: 
        inspector = inspect(db_engine)
     
        # TABLE_LIST is supplied as a comma-separated environment value.
        generated_contracts = []

        for table_name in table_list:

            try:
                result = generate_contract(
                    inspector,
                    engine_type,
                    database_name,
                    schema_name,
                    table_name,
                    service,
                    schema_registry,
                    audit_table,
                    contract_version,
                    version,
                    namespace,
                    execution_id,
                    )
                generated_contracts.append(result)

            except Exception:
                logger.exception(
                    "Failed processing table %s",
                    table_name
                )
                raise
    

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": (
                    "Schema generation completed successfully"
                ),
                "contracts": generated_contracts,
            })
        }

    finally:
        logger.info(
            "Disposing database engine",
            extra={
                "database_name": database_name,
                "engine": engine_type,
            },
        )
        db_engine.dispose()