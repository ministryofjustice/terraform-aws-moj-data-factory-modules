import json
import hashlib
import os
import boto3
import urllib.parse
import uuid
from logger import get_logger
from datetime import datetime, timezone
from fastavro.schema import to_parsing_canonical_form

logger = get_logger('schema-registration')

s3 = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')

AUDIT_TABLE_NAME = os.environ.get(
    "AUDIT_TABLE_NAME",
    "contract_lifecycle_audit_2" # default value linking to the testing DynamoDB table
)

audit_table = dynamodb.Table(AUDIT_TABLE_NAME)

def lambda_handler(event, context):
    try:
        # Loop is required because AWS passes S3 records inside a list wrapper
        for record in event['Records']:
            object_key = urllib.parse.unquote_plus(record['s3']['object']['key'])
            bucket_name = record['s3']['bucket']['name']

            logger.info("Schema registration started.",
                extra={
                    'execution_id': context.aws_request_id,
                    'object_key': object_key
                }
            )
            s3_response = s3.get_object(Bucket=bucket_name, Key=object_key)
            file_bytes = s3_response["Body"].read()
            metadata = json.loads(file_bytes.decode("utf-8"))
            canonical_schema = to_parsing_canonical_form(metadata)
            schema_fingerprint = hashlib.sha256(canonical_schema.encode("utf-8")).hexdigest()

            required_keys = ["source", "schema_name", "database_name", "table_name", "contract_version"]
            missing_keys = [key for key in required_keys if key not in metadata or metadata[key] is None or metadata[key] == ""]

            if missing_keys:
                raise ValueError(f"Missing required metadata fields: {', '.join(missing_keys)}")

            # breaking metadata down into local variables for cleaner use
            source_name = metadata["source"]
            schema_name = metadata["schema_name"]
            db_name = metadata["database_name"]
            table_name = metadata["table_name"]
            contract_version = metadata["contract_version"]
            file_name = os.path.basename(object_key)

            contract_uri = f"s3://{bucket_name}/{source_name}/{db_name}/{schema_name}/{table_name}/{contract_version}/{file_name}"
           
            contract_id = str(uuid.uuid5(uuid.NAMESPACE_DNS,f"{source_name}:{schema_name}:{db_name}:{table_name}"))

            event_time = datetime.now(timezone.utc).isoformat()

            logger.info(f"""schema_fingerprint :::{schema_fingerprint} 
                        contract_uri  ::: {contract_uri}
                        contract_id :::: {contract_id}
                        event_time :::: {event_time}
                        source_name :::: {source_name}
                        schema_name :::: {schema_name}
                        db_name :::: {db_name}
                        table_name :::: {table_name}
                        contract_version :::: {contract_version}""",
                extra={
                    'execution_id': context.aws_request_id
                }
            )
            
            audit_table.put_item(
                Item={
                    
                    'contract_id': contract_id,
                    'ingestion_version':"",
                    'registered_at': event_time,
                    'contract_version': contract_version,
                    'source_name': source_name,
                    'schema_name': schema_name,
                    'db_name': db_name,
                    'table_name': table_name,
                    'schema_fingerprint': schema_fingerprint,
                    's3_contract_uri': contract_uri,
                    'is_active': "no"
                }
            )

            logger.info(f"Schema registration successful.",
                extra={
                    'execution_id': context.aws_request_id
                }
            )
            
        return {
            'statusCode': 200,
            'body': json.dumps('Successfully entered the schema into the DynamoDB audit table.')
        }
        
    except Exception as e:
        logger.error(f"Error registering schema: {str(e)}",
            extra={
                'execution_id': context.aws_request_id
            }
        )
        raise e