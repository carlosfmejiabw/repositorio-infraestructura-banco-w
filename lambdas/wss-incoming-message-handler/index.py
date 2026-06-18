import json
import os
from datetime import datetime
import boto3
from aws_lambda_powertools import Logger

logger = Logger(log_uncaught_exceptions=True)
sns = boto3.client("sns", region_name=os.environ["AWS_REGION"])


def handle_message(conn_id, body, user_id):
    action = body["action"]
    model_interface = body.get("modelInterface", "langchain")
    agentcore_runtime_arn = body.get("agentcoreRuntimeArn")  
    
    data = body.get("data", {})
    return handle_request(conn_id, action, model_interface, data, user_id, agentcore_runtime_arn)


def handle_request(conn_id, action, model_interface, data, user_id, agentcoreRuntimeArn):
    message = {
        "action": action,
        "modelInterface": model_interface,
        "direction": "IN",
        "source": "WEB",
        "agentcoreRuntimeArn": agentcoreRuntimeArn,
        "connectionId": conn_id,
        "timestamp": str(int(round(datetime.now().timestamp()))),
        "userId": user_id,
        "data": data,
    }
    response = sns.publish(
        TopicArn=os.environ["MESSAGES_TOPIC_ARN"],
        Message=json.dumps(message),
    )
    return {"statusCode": 200, "body": json.dumps(response)}


@logger.inject_lambda_context(log_event=False)
def handler(event, context):
    event_type = event["requestContext"]["eventType"]
    connection_id = event["requestContext"]["connectionId"]
    user_id = event["requestContext"]["connectionId"]

    logger.set_correlation_id(connection_id)

    if event_type == "MESSAGE":
        message = json.loads(event["body"])
        return handle_message(connection_id, message, user_id)  
    
    return {"StatusCode": 400, "body": json.dumps({"message": f"Unhandled event type {event_type}"})}