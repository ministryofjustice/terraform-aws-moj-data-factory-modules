


import os
import json

import boto3
from sqlalchemy import create_engine, inspect

db_secret_arn = os.getenv("DB_SECRET_ARN")

schema_registry_bucket = os.getenv("SCHEMA_REGISTRY_BUCKET")

namespace = os.getenv("CONTRACT_NAMESPACE", "")
service = os.getenv("CONTRACT_SERVICE")

#secret manager client
def _get_secretmanager():
    return boto3.client("secretsmanager")

def _get_s3():
    return boto3.client("s3")

def get_database_secret():
#secret manager client stored in a variable
    secretsmanager = _get_secretmanager()

#secret details stored in variable 
    db_secret_response = secretsmanager.get_secret_value(SecretId=db_secret_arn)

#secret string converted to python dictionary (key: value) and stored in db_secret variable 
    db_secret = json.loads(db_secret_response["SecretString"])

    return db_secret

def convert_postgres_type(column_type):
    type_name = str(column_type).lower()

    if "uuid" in type_name:
        return {"type": "string", "logicalType": "UUID"}
    elif "bigint" in type_name:
        return "long"
    elif "smallint" in type_name or "integer" in type_name:
        return "int"
    elif "boolean" in type_name:
        return "boolean"
    elif "timestamp" in type_name:
        return {"type": "long", "logicalType": "timestamp-micros"}
    elif "date" in type_name:
        return {"type": "int", "logicalType": "date"}
    elif "double" in type_name:
        return "double"
    elif "real" in type_name or "float" in type_name:
        return "float"
    elif "numeric" in type_name or "decimal" in type_name:
        return "double"
    elif "char" in type_name or "text" in type_name:
        return "string"
    else:
        raise ValueError(f"Unsupported PostgreSQL type: {column_type}")


def convert_oracle_type(column_type):
    type_name = str(column_type).lower()

    if "number" in type_name:
        return "double"
    elif "integer" in type_name:
        return "int"
    elif "float" in type_name:
        return "float"
    elif "timestamp" in type_name:
        return {"type": "long", "logicalType": "timestamp-micros"}
    elif "date" in type_name:
        return {"type": "long", "logicalType": "timestamp-micros"}
    elif "char" in type_name or "clob" in type_name:
        return "string"
    elif "blob" in type_name or "raw" in type_name:
        return "bytes"
    else:
        raise ValueError(f"Unsupported Oracle type: {column_type}")

def build_avro_contract(
    namespace,
    service,
    table_name,
    database_name,
    database_type,
    avro_fields
):
    return {
        "type": "record",
        "namespace": namespace,
        "service": service,
        "version": "1.0.0",
        "name": table_name,
        "description": "",
        "contacts": {
            "team": "https://dsdmoj.atlassian.net/wiki/spaces/DPR/overview?homepageId=4015489077",
            "slack": "#ask_dpr"
        },
        "database": {
            "type": database_type,
            "name": database_name
        },
        "fields": avro_fields
    }

def validate_contract(contract):
    required_fields = [
        "type",
        "name",
        "version",
        "database",
        "fields",
        "service",
    ]

    for field in required_fields:
        if field not in contract:
            raise ValueError(f"Contract is missing required field: {field}")

    if contract["type"] != "record":
        raise ValueError("Contract type must be 'record'")

    if not contract["fields"]:
        raise ValueError("Contract must contain at least one field")

    return True


def store_contract(contract_json, s3_key):
    s3 = _get_s3()

    s3.put_object(
        Bucket=schema_registry_bucket,
        Key=s3_key,
        Body=contract_json,
        ContentType="application/json"
    )


def lambda_handler(event, context):
    table_name = "dms_integration_test"
    db_secret = get_database_secret()


    engine_type = db_secret.get("engine", os.getenv("ENGINE"))

    if engine_type == "oracle":
        schema_name = "DMS_USER"
    else:
        schema_name = "public"

    username = db_secret["username"]
    password = db_secret["password"]
    host = db_secret["host"]
    db_name = db_secret.get("dbname", os.getenv("DATABASE_NAME"))

    if engine_type == "oracle":
        port = db_secret.get("port", 1521)
        sid = db_secret.get("sid", os.getenv("DATABASE_SID"))

        db_string = (
            f"oracle+oracledb://{username}:{password}"
            f"@{host}:{port}/?service_name={sid}"
        )
    else:
        port = db_secret.get("port", 5432)

        db_string = (
            f"postgresql://{username}:{password}"
            f"@{host}:{port}/{db_name}"
        )

    engine = create_engine(db_string)
    inspector = inspect(engine)

    columns = inspector.get_columns(
        table_name,
        schema=schema_name
    )

    primary_key = inspector.get_pk_constraint(
        table_name,
        schema=schema_name
    )

    primary_key_columns = primary_key.get("constrained_columns", [])

    avro_fields = []

    for column in columns:

        if engine_type == "oracle":
            avro_type = convert_oracle_type(column["type"])
        else:
            avro_type = convert_postgres_type(column["type"])

        field = {
            "name": column["name"],
            "type": avro_type,
            "nullable": column["nullable"]
        }

        if column["name"] in primary_key_columns:
            field["key"] = "primary"

        avro_fields.append(field)

    contract = build_avro_contract(
        namespace=namespace,
        service=service,
        table_name=table_name,
        database_name=db_name,
        database_type=engine_type,
        avro_fields=avro_fields
    )

    validate_contract(contract)

    contract_json = json.dumps(contract, indent=2)

# TODO: Confirm expected source value for schema registry S3 path
    s3_key = (
        f"{engine_type}/{db_name}/{schema_name}/"
        f"{table_name}/v1/contract.avsc"
    )

    store_contract(contract_json, s3_key)

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Schema generated and stored successfully",
            "s3_key": s3_key
        })
    }