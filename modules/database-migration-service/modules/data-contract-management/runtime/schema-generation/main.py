import json
import os
import re

import hashlib
import boto3
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import URL
from sqlalchemy import types as sql_types
from sqlalchemy.dialects import oracle, postgresql
from fastavro.schema import parse_schema
from botocore.exceptions import ClientError
from logger import get_logger
from urllib.parse import quote


logger = get_logger("schema-generation")

s3 = boto3.client("s3")

SUPPORTED_ENGINES = {
"postgres",
"oracle",
}

def validate_contacts(contacts):
    if not isinstance(contacts, dict):
        raise ValueError(
            "Contract contacts must be a JSON object."
        )

    for key, value in contacts.items():
        if (
            not isinstance(key, str)
            or not key.strip()
            or key != key.strip()
        ):
            raise ValueError(
                "Contact names must be non-empty strings "
                "without leading or trailing whitespace."
            )

        if (
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
        ):
            raise ValueError(
                f"Contact '{key}' must contain a non-empty string "
                "without leading or trailing whitespace."
            )


def validate_configuration():
    required_names = (
        "DB_SECRET_ARN",
        "SCHEMA_REGISTRY_BUCKET",
        "ENGINE",
        "CONTRACT_NAMESPACE",
        "CONTRACT_SERVICE",
        "DATABASE_NAME",
        "SCHEMA_NAME",
        "CONTRACT_VERSION",
        "TABLE_LIST",
    )

    config = {
        name: os.getenv(name)
        for name in required_names
    }

    missing_names = [
        name
        for name, value in config.items()
        if value is None or not value.strip()
    ]

    if missing_names:
        raise ValueError(
            "Missing required environment variables: "
            + ", ".join(sorted(missing_names))
        )

    for name, value in config.items():
        if value != value.strip():
            raise ValueError(
                f"{name} must not contain leading or trailing whitespace."
            )

    config["ENGINE"] = config["ENGINE"].lower()

    if config["ENGINE"] not in SUPPORTED_ENGINES:
        raise ValueError(
            "Unsupported ENGINE. Expected postgres or oracle."
        )

    if not re.fullmatch(
        r"v[1-9][0-9]*",
        config["CONTRACT_VERSION"],
    ):
        raise ValueError(
            "Invalid CONTRACT_VERSION. Expected v1, v2, v3, etc. "
            "Use lowercase v with no leading zeros. "
            "v0 is reserved for future migration."
        )

    namespace_parts = config["CONTRACT_NAMESPACE"].split(".")

    if not all(
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part)
        for part in namespace_parts
    ):
        raise ValueError(
            "CONTRACT_NAMESPACE must contain valid Avro identifiers "
            "separated by dots, for example uk.gov.justice.contracts."
        )

    for name in (
        "CONTRACT_SERVICE",
        "DATABASE_NAME",
        "SCHEMA_NAME",
    ):
        if ":" in config[name]:
            raise ValueError(
                f"{name} must not contain ':', because registration "
                "uses it to separate contract identity fields."
            )

    raw_tables = config["TABLE_LIST"].split(",")

    if any(not table.strip() for table in raw_tables):
        raise ValueError(
            "TABLE_LIST must contain comma-separated table names "
            "without empty entries."
        )

    tables = [table.strip() for table in raw_tables]

    if len(tables) != len(set(tables)):
        raise ValueError(
            "TABLE_LIST must not contain duplicate table names."
        )

    for table in tables:
        if ":" in table:
            raise ValueError(
                f"Table name '{table}' must not contain ':', because "
                "registration uses it to separate identity fields."
            )

    config["TABLE_LIST"] = tables

    if config["ENGINE"] == "oracle":
        service_name = os.getenv("DATABASE_SERVICE_NAME")
        sid = os.getenv("DATABASE_SID")

        for name, value in (
            ("DATABASE_SERVICE_NAME", service_name),
            ("DATABASE_SID", sid),
        ):
            if value is not None:
                if not value.strip() or value != value.strip():
                    raise ValueError(
                        f"{name} must contain a non-empty value "
                        "without leading or trailing whitespace."
                    )

        if (service_name is None) == (sid is None):
            raise ValueError(
                "For Oracle, configure exactly one of "
                "DATABASE_SERVICE_NAME or DATABASE_SID."
            )

        config["DATABASE_SERVICE_NAME"] = service_name
        config["DATABASE_SID"] = sid

    raw_contacts = os.getenv("CONTRACT_CONTACTS", "{}")

    try:
        contacts = json.loads(raw_contacts)
    except json.JSONDecodeError as error:
        raise ValueError(
            "CONTRACT_CONTACTS must contain a valid JSON object."
        ) from error

    validate_contacts(contacts)
    config["CONTRACT_CONTACTS"] = contacts

    return config



def get_database_secret(db_secret_arn):
    secretsmanager = boto3.client("secretsmanager")

    response = secretsmanager.get_secret_value(
        SecretId=db_secret_arn
    )

    secret_string = response.get("SecretString")

    if not secret_string:
        raise ValueError(
            "Database secret must contain a JSON SecretString."
        )

    try:
        secret = json.loads(secret_string)
    except json.JSONDecodeError as error:
        raise ValueError(
            "Database secret contains invalid JSON."
        ) from error

    if not isinstance(secret, dict):
        raise ValueError(
            "Database secret must contain a JSON object."
        )

    invalid_fields = [
        field
        for field in ("username", "password", "host")
        if not isinstance(secret.get(field), str)
        or not secret[field]
    ]

    if invalid_fields:
        raise ValueError(
            "Database secret must contain non-empty strings for: "
            + ", ".join(invalid_fields)
        )

    for field in ("username", "host"):
        if (
            not secret[field].strip()
            or secret[field] != secret[field].strip()
        ):
            raise ValueError(
                f"Database secret field '{field}' must not be blank "
                "or contain leading or trailing whitespace."
            )

    return secret

def build_database_connection(config, db_secret):
    engine = config["ENGINE"]

    if engine not in SUPPORTED_ENGINES:
        raise ValueError(
            "Unsupported ENGINE. Expected postgres or oracle."
        )

    default_port = 1521 if engine == "oracle" else 5432
    raw_port = db_secret.get("port", default_port)

    if isinstance(raw_port, bool) or not isinstance(
        raw_port, (int, str)
    ):
        raise ValueError(
            "Database port must be an integer between 1 and 65535."
        )

    if isinstance(raw_port, str) and not re.fullmatch(
        r"[0-9]+", raw_port
    ):
        raise ValueError(
            "Database port must be an integer between 1 and 65535."
        )

    port = int(raw_port)

    if not 1 <= port <= 65535:
        raise ValueError(
            "Database port must be between 1 and 65535."
        )

    connection_settings = {
        "username": db_secret["username"],
        "password": db_secret["password"],
        "host": db_secret["host"],
        "port": port,
    }

    if engine == "postgres":
        return URL.create(
            drivername="postgresql+psycopg2",
            database=config["DATABASE_NAME"],
            **connection_settings,
        )

    service_name = config.get("DATABASE_SERVICE_NAME")
    sid = config.get("DATABASE_SID")

    if service_name:
        return URL.create(
            drivername="oracle+oracledb",
            query={"service_name": service_name},
            **connection_settings,
        )

    if sid:
        return URL.create(
            drivername="oracle+oracledb",
            database=sid,
            **connection_settings,
        )

    raise ValueError(
        "Oracle requires DATABASE_SERVICE_NAME or DATABASE_SID."
    )


def create_database_engine(
    connection_url: URL,
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


def build_decimal_type(precision, scale):
    if (
        isinstance(precision, bool)
        or not isinstance(precision, int)
        or precision < 1
    ):
        raise ValueError(
            "Decimal mapping requires a positive, explicit precision."
        )

    if scale is None:
        scale = 0

    if isinstance(scale, bool) or not isinstance(scale, int):
        raise ValueError(
            "Decimal mapping requires an integer scale."
        )

    if scale < 0:
        precision -= scale
        scale = 0
    else:
        precision = max(precision, scale)

    return {
        "type": "bytes",
        "logicalType": "decimal",
        "precision": precision,
        "scale": scale,
    }


def convert_postgres_type(column_type):
    if isinstance(
        column_type,
        (sql_types.ARRAY, sql_types.Enum),
    ):
        raise ValueError(
            f"PostgreSQL type '{column_type}' requires an explicit "
            "mapping and is not supported yet."
        )

    if isinstance(column_type, sql_types.Uuid):
        return {
            "type": "string",
            "logicalType": "uuid",
        }

    if isinstance(column_type, sql_types.Boolean):
        return "boolean"

    if isinstance(column_type, sql_types.BigInteger):
        return "long"

    if isinstance(column_type, sql_types.Integer):
        return "int"

    if isinstance(column_type, postgresql.REAL):
        return "float"

    if isinstance(column_type, postgresql.DOUBLE_PRECISION):
        return "double"

    if isinstance(column_type, sql_types.Float):
        precision = column_type.precision

        if precision is not None and not 1 <= precision <= 53:
            raise ValueError(
                "PostgreSQL floating-point precision must be "
                "between 1 and 53."
            )

        return (
            "float"
            if precision is not None and precision <= 24
            else "double"
        )

    if isinstance(column_type, sql_types.Numeric):
        return build_decimal_type(
            column_type.precision,
            column_type.scale,
        )

    if isinstance(column_type, sql_types.DateTime):
        return {
            "type": "long",
            "logicalType": (
                "timestamp-micros"
                if column_type.timezone
                else "local-timestamp-micros"
            ),
        }

    if isinstance(column_type, sql_types.Date):
        return {
            "type": "int",
            "logicalType": "date",
        }

    if isinstance(column_type, sql_types.Time):
        if column_type.timezone:
            raise ValueError(
                "PostgreSQL TIME WITH TIME ZONE requires an explicit "
                "mapping and is not supported yet."
            )

        return {
            "type": "long",
            "logicalType": "time-micros",
        }

    if isinstance(column_type, sql_types.LargeBinary):
        return "bytes"

    if isinstance(column_type, sql_types.String):
        return "string"

    raise ValueError(
        f"Unsupported PostgreSQL column type: {column_type}."
    )



def convert_oracle_type(column_type):
    if isinstance(column_type, oracle.BINARY_FLOAT):
        return "float"

    if isinstance(column_type, oracle.BINARY_DOUBLE):
        return "double"

    if isinstance(column_type, sql_types.Float):
        raise ValueError(
            "Oracle FLOAT requires an agreed precision-preserving "
            "mapping. It cannot automatically be treated as an "
            "Avro float or double."
        )

    if isinstance(column_type, sql_types.Numeric):
        precision = column_type.precision
        scale = column_type.scale

        if precision is None:
            raise ValueError(
                "Oracle NUMBER without explicit precision is not "
                "supported yet. An agreed lossless mapping is required."
            )

        if scale in (None, 0):
            if 1 <= precision <= 9:
                return "int"

            if 10 <= precision <= 18:
                return "long"

        return build_decimal_type(precision, scale)

    if isinstance(column_type, sql_types.Integer):
        return build_decimal_type(38, 0)

    if isinstance(column_type, oracle.TIMESTAMP):
        if column_type.local_timezone:
            raise ValueError(
                "Oracle TIMESTAMP WITH LOCAL TIME ZONE requires "
                "an explicit session-timezone policy before mapping."
            )

        return {
            "type": "long",
            "logicalType": (
                "timestamp-micros"
                if column_type.timezone
                else "local-timestamp-micros"
            ),
        }

    if isinstance(column_type, oracle.DATE):
        return {
            "type": "long",
            "logicalType": "local-timestamp-micros",
        }

    if isinstance(column_type, sql_types.LargeBinary):
        return "bytes"

    if isinstance(column_type, oracle.RAW):
        return "bytes"

    if isinstance(column_type, sql_types.String):
        return "string"

    raise ValueError(
        f"Unsupported Oracle column type: {column_type}."
    )

def validate_avro_name(value, label):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value)
    ):
        raise ValueError(
            f"{label} '{value}' is not a valid Avro name. "
            "Use a letter or underscore first, followed by "
            "letters, numbers or underscores. "
            "Source identifiers requiring renaming are not "
            "supported yet."
        )


def build_avro_contract(
    namespace,
    service,
    table_name,
    schema_name,
    database_name,
    engine_type,
    contract_version,
    avro_fields,
    contacts,
):
    validate_avro_name(table_name, "Table name")
    validate_contacts(contacts)

    return {
        "type": "record",
        "namespace": namespace,
        "source": service,
        "service": service,
        "contract_version": contract_version,
        "name": table_name,
        "database_name": database_name,
        "schema_name": schema_name,
        "table_name": table_name,
        "description": "",
        "contacts": dict(contacts),
        "database": {
            "type": engine_type,
            "name": database_name,
        },
        "fields": avro_fields,
    }




# Read table metadata
def get_table_metadata(inspector, schema_name, table_name):
    if not inspector.has_table(
        table_name,
        schema=schema_name,
    ):
        raise ValueError(
            f"Source table '{schema_name}.{table_name}' "
            "does not exist or is not visible to the database user."
        )

    columns = inspector.get_columns(
        table_name,
        schema=schema_name,
    )

    primary_key = inspector.get_pk_constraint(
        table_name,
        schema=schema_name,
    )

    primary_key_columns = (
        primary_key.get("constrained_columns") or []
    )

    if not primary_key_columns:
        logger.warning(
            "Source table has no primary key.",
            extra={
                "schema_name": schema_name,
                "table_name": table_name,
            },
        )

    return columns, primary_key_columns


#Convert database columns to Avro fields
def build_avro_fields(
    columns,
    primary_key_columns,
    engine_type,
):
    converters = {
        "postgres": convert_postgres_type,
        "oracle": convert_oracle_type,
    }

    converter = converters.get(engine_type)

    if converter is None:
        raise ValueError(
            f"Unsupported database engine: {engine_type}."
        )

    if not columns:
        raise ValueError(
            "Cannot generate a contract for a table with no columns."
        )

    primary_keys = set(primary_key_columns)
    seen_names = set()
    avro_fields = []

    for column in columns:
        name = column.get("name")
        validate_avro_name(name, "Column name")

        if name in seen_names:
            raise ValueError(
                f"Duplicate column name in source metadata: '{name}'."
            )

        seen_names.add(name)

        if "type" not in column:
            raise ValueError(
                f"Column '{name}' is missing type metadata."
            )

        nullable = column.get("nullable")

        if not isinstance(nullable, bool):
            raise ValueError(
                f"Column '{name}' is missing valid nullable metadata."
            )

        try:
            avro_type = converter(column["type"])
        except ValueError as error:
            raise ValueError(
                f"Cannot map column '{name}': {error}"
            ) from error

        field = {
            "name": name,
            "type": (
                ["null", avro_type]
                if nullable
                else avro_type
            ),
        }

        if nullable:
            field["default"] = None

        if name in primary_keys:
            field["key"] = "primary"

        avro_fields.append(field)

    missing_primary_keys = primary_keys - seen_names

    if missing_primary_keys:
        raise ValueError(
            "Primary-key metadata references missing columns: "
            + ", ".join(sorted(missing_primary_keys))
        )

    return avro_fields



def validate_contract(contract):
    if not isinstance(contract, dict):
        raise ValueError(
            "Contract must be a JSON object."
        )

    required_strings = (
        "name",
        "namespace",
        "source",
        "service",
        "contract_version",
        "database_name",
        "schema_name",
        "table_name",
    )

    for name in required_strings:
        value = contract.get(name)

        if (
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
        ):
            raise ValueError(
                f"Contract '{name}' must contain a non-empty string "
                "without leading or trailing whitespace."
            )

    if contract.get("type") != "record":
        raise ValueError(
            "Contract type must be 'record'."
        )

    if not re.fullmatch(
        r"v[1-9][0-9]*",
        contract["contract_version"],
    ):
        raise ValueError(
            "Contract version must be v1 onwards, using lowercase v "
            "and no leading zeros. v0 is reserved for migration."
        )

    for name in (
        "source",
        "database_name",
        "schema_name",
        "table_name",
    ):
        if ":" in contract[name]:
            raise ValueError(
                f"Contract '{name}' must not contain ':'."
            )

    validate_avro_name(contract["name"], "Record name")

    for part in contract["namespace"].split("."):
        validate_avro_name(part, "Namespace segment")

    if contract["name"] != contract["table_name"]:
        raise ValueError(
            "Generated record name must match table_name."
        )

    if contract["source"] != contract["service"]:
        raise ValueError(
            "Generated source must match service."
        )

    database = contract.get("database")

    if (
        not isinstance(database, dict)
        or database.get("type") not in SUPPORTED_ENGINES
        or database.get("name") != contract["database_name"]
    ):
        raise ValueError(
            "Contract database metadata must contain a supported "
            "engine and match database_name."
        )

    validate_contacts(contract.get("contacts"))

    fields = contract.get("fields")

    if not isinstance(fields, list) or not fields:
        raise ValueError(
            "Contract fields must be a non-empty list."
        )

    seen_names = set()

    for field in fields:
        if not isinstance(field, dict):
            raise ValueError(
                "Each contract field must be a JSON object."
            )

        name = field.get("name")
        validate_avro_name(name, "Field name")

        if name in seen_names:
            raise ValueError(
                f"Duplicate contract field name: '{name}'."
            )

        seen_names.add(name)

        if "type" not in field:
            raise ValueError(
                f"Contract field '{name}' is missing its type."
            )


def validate_avro_contract(contract):
    validate_contract(contract)

    schema_copy = json.loads(
        json.dumps(
            contract,
            allow_nan=False,
        )
    )

    parse_schema(schema_copy)


def normalise_contract(contract):
    return json.dumps(
        contract,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def calculate_contract_fingerprint(contract):
    normalised = normalise_contract(contract)

    return hashlib.sha256(
        normalised.encode("utf-8")
    ).hexdigest()


def save_contract(schema_registry_bucket, contract):
    validate_avro_contract(contract)

    path_parts = (
        contract["source"],
        contract["database_name"],
        contract["schema_name"],
        contract["table_name"],
        contract["contract_version"],
    )

    object_key = "/".join(
        quote(part, safe="")
        for part in path_parts
    ) + "/contract.avsc"

    fingerprint = calculate_contract_fingerprint(contract)

    result = {
        "bucket_name": schema_registry_bucket,
        "s3_key": object_key,
        "s3_contract_uri": (
            f"s3://{schema_registry_bucket}/{object_key}"
        ),
        "schema_fingerprint": fingerprint,
    }

    try:
        response = s3.put_object(
            Bucket=schema_registry_bucket,
            Key=object_key,
            Body=normalise_contract(contract).encode("utf-8"),
            ContentType="application/avro+json",
            IfNoneMatch="*",
        )

    except ClientError as error:
        error_code = error.response.get("Error", {}).get("Code")
        http_status = error.response.get(
            "ResponseMetadata", {}
        ).get("HTTPStatusCode")

        if (
            error_code not in ("PreconditionFailed", "412")
            and http_status != 412
        ):
            raise

        response = s3.get_object(
            Bucket=schema_registry_bucket,
            Key=object_key,
        )

        body = response["Body"]

        try:
            existing_contract = json.loads(
                body.read().decode("utf-8")
            )
        finally:
            body.close()

        existing_fingerprint = calculate_contract_fingerprint(
            existing_contract
        )

        if existing_fingerprint != fingerprint:
            raise ValueError(
                f"Contract '{object_key}' already exists "
                "with different content. Supply a new contract "
                "version through the contract lifecycle process."
            ) from error

        result["status"] = "ALREADY_EXISTS"

    else:
        result["status"] = "STORED"

    version_id = response.get("VersionId")

    if version_id is not None:
        result["s3_object_version_id"] = version_id

    return result


def generate_contract(
    inspector,
    engine_type: str,
    database_name: str,
    schema_name: str,
    table_name: str,
    service: str,
    schema_registry_bucket: str,
    contract_version: str,
    namespace: str,
    execution_id: str,
    contacts: dict,
):
    log_context = {
        "execution_id": execution_id,
        "source": service,
        "database_name": database_name,
        "schema_name": schema_name,
        "table_name": table_name,
        "contract_version": contract_version,
    }

    logger.info(
        "Schema generation started.",
        extra=log_context,
    )

    columns, primary_key_columns = get_table_metadata(
        inspector,
        schema_name,
        table_name,
    )

    logger.info(
        "Source metadata retrieved.",
        extra={
            **log_context,
            "column_count": len(columns),
        },
    )

    avro_fields = build_avro_fields(
        columns,
        primary_key_columns,
        engine_type,
    )

    contract = build_avro_contract(
        namespace=namespace,
        service=service,
        table_name=table_name,
        schema_name=schema_name,
        database_name=database_name,
        engine_type=engine_type,
        contract_version=contract_version,
        avro_fields=avro_fields,
        contacts=contacts,
    )

    publication = save_contract(
        schema_registry_bucket,
        contract,
    )

    logger.info(
        "Schema generation completed.",
        extra={
            **log_context,
            "publication_status": publication["status"],
            "object_key": publication["s3_key"],
            "s3_object_version_id": publication.get(
                "s3_object_version_id"
            ),
        },
    )

    return {
        "source": service,
        "database_name": database_name,
        "schema_name": schema_name,
        "table_name": table_name,
        "contract_version": contract_version,
        **publication,
    }


def lambda_handler(event, context):
    execution_id = getattr(context, "aws_request_id", None)

    log_context = {
        "execution_id": execution_id,
    }

    db_engine = None

    try:
        config = validate_configuration()

        log_context.update(
            {
                "engine": config["ENGINE"],
                "source": config["CONTRACT_SERVICE"],
                "database_name": config["DATABASE_NAME"],
                "schema_name": config["SCHEMA_NAME"],
                "contract_version": config["CONTRACT_VERSION"],
            }
        )

        logger.info(
            "Schema generation configuration loaded.",
            extra={
                **log_context,
                "table_count": len(config["TABLE_LIST"]),
            },
        )

        db_secret = get_database_secret(
            config["DB_SECRET_ARN"]
        )

        connection_url = build_database_connection(
            config,
            db_secret,
        )

        db_engine = create_database_engine(
            connection_url,
            config["ENGINE"],
            config["DATABASE_NAME"],
        )

        generated_contracts = []
        failed_tables = []
        last_error = None

        with db_engine.connect() as connection:
            inspector = inspect(connection)

            for table_name in config["TABLE_LIST"]:
                try:
                    result = generate_contract(
                        inspector=inspector,
                        engine_type=config["ENGINE"],
                        database_name=config["DATABASE_NAME"],
                        schema_name=config["SCHEMA_NAME"],
                        table_name=table_name,
                        service=config["CONTRACT_SERVICE"],
                        schema_registry_bucket=config[
                            "SCHEMA_REGISTRY_BUCKET"
                        ],
                        contract_version=config[
                            "CONTRACT_VERSION"
                        ],
                        namespace=config["CONTRACT_NAMESPACE"],
                        execution_id=execution_id,
                        contacts=config["CONTRACT_CONTACTS"],
                    )

                    generated_contracts.append(result)

                except Exception as error:
                    logger.exception(
                        "Table contract generation failed.",
                        extra={
                            **log_context,
                            "table_name": table_name,
                        },
                    )

                    failed_tables.append(table_name)
                    last_error = error

                    connection.rollback()

        if failed_tables:
            raise RuntimeError(
                "Schema generation failed for "
                f"{len(failed_tables)} table(s): "
                + ", ".join(failed_tables)
                + ". Successfully published contracts were retained."
            ) from last_error

        logger.info(
            "Schema generation completed successfully.",
            extra={
                **log_context,
                "contract_count": len(generated_contracts),
            },
        )

        return {
            "status": "SUCCEEDED",
            "execution_id": execution_id,
            "contracts": generated_contracts,
        }

    except Exception:
        logger.exception(
            "Schema generation invocation failed.",
            extra=log_context,
        )
        raise

    finally:
        if db_engine is not None:
            db_engine.dispose()