import json
import os
import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.batch import BatchProcessor, EventType
from aws_lambda_powertools.utilities.batch.exceptions import BatchProcessingError
from aws_lambda_powertools.utilities.data_classes.sqs_event import SQSRecord


logger = Logger(log_uncaught_exceptions=True)
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["CONNECTIONS_TABLE_NAME"])

processor = BatchProcessor(event_type=EventType.SQS)
logger = Logger()

api_gateway_management_api = boto3.client("apigatewaymanagementapi", endpoint_url=os.environ["WEBSOCKET_API_ENDPOINT"])




def record_handler(record=SQSRecord):
    payload: str = record.body
    message: dict = json.loads(payload)
    detail: dict = json.loads(message["Message"])
    connection_id = detail["connectionId"]

    try:
        api_gateway_management_api.post_to_connection(ConnectionId=connection_id, Data=json.dumps(detail))
    except Exception as e:
        logger.info(f"Exception while sending message to connection {connection_id} for user : {e}")



@logger.inject_lambda_context(log_event=False)
def handler(event, context):

    batch = event["Records"]
    try:
        with processor(records=batch, handler=record_handler):
            processed_messages = processor.process()
    except BatchProcessingError as e:
        logger.error(e)

    return processor.response()