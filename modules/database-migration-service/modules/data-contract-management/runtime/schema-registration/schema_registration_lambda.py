import json
import hashlib
import boto3
import urllib.parse
import uuid
from logger import get_logger
from datetime import datetime, timezone

logger = get_logger('schema-registration')

s3 = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')

# TODO: Make this table name an environment variable so that it can be changed without code changes
audit_table = dynamodb.Table('contract_lifecycle_audit_2')

def read_avsc_metadata(content):
    decoded_content = content.decode('utf-8')
    return json.loads(decoded_content)

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
            file_bytes = s3_response['Body'].read()

            schema_hash = hashlib.sha256(file_bytes).hexdigest()
            entry_id = str(uuid.uuid4())

            metadata = read_avsc_metadata(file_bytes)

            required_keys = ["source", "schema_name", "database_name", "table_name", "version"]
            missing_keys = [key for key in required_keys if key not in metadata]

            if missing_keys:
                raise ValueError(f"Missing required metadata fields: {', '.join(missing_keys)}")

            # breaking metadata down into local variables for cleaner use
            source_name = metadata["source"]
            schema_name = metadata["schema_name"]
            db_name = metadata["database_name"]
            table_name = metadata["table_name"]
            version = metadata["version"]

            contract_uri = f"s3://data-factory-moj-development-schema-registry/{source_name}/{db_name}/{schema_name}/{table_name}/{version}/{object_key}"
            
            event_time = datetime.now(timezone.utc).isoformat()
            
            audit_table.put_item(
                Item={
                    'contract_id': entry_id,
                    'registered_at': event_time,
                    'contract_version': version,
                    'source_name': source_name,
                    'schema_name': schema_name,
                    'db_name': db_name,
                    'table_name': table_name,
                    'schema_footprint': schema_hash,
                    's3_contract_uri': contract_uri
                }
            )

            logger.info(f"Schema registration successful.",
                extra={
                    'execution_id': context.aws_request_id,
                    'contract_uri': contract_uri,
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
        return {
            'statusCode': 500,
            'body': json.dumps(f"Error: {str(e)}")
        }
