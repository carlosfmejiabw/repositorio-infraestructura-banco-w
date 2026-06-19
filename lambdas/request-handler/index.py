"""
Simplified Request Handler for AWS Bedrock AgentCore Integration

This Lambda function:
1. Receives messages from SQS
2. Invokes AgentCore Runtime with the request
3. Streams responses back via SNS -> WebSocket

Replaces the previous complex adapter system with a simple AgentCore invocation.
"""

import os
import json
import uuid
from datetime import datetime
from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.utilities import parameters
from aws_lambda_powertools.utilities.batch import BatchProcessor, EventType
from aws_lambda_powertools.utilities.batch.exceptions import BatchProcessingError
from aws_lambda_powertools.utilities.data_classes.sqs_event import SQSRecord
from aws_lambda_powertools.utilities.typing import LambdaContext

from genai_core.utils.websocket import send_to_client
from genai_core.types import ChatbotAction

import boto3

processor = BatchProcessor(event_type=EventType.SQS)
tracer = Tracer()
logger = Logger()

# Environment variables
AWS_REGION = os.environ["AWS_REGION"]
AGENTCORE_MEMORY_ID = os.environ.get("AGENTCORE_MEMORY_ID")
AGENTCORE_GATEWAY_ID = os.environ.get("AGENTCORE_GATEWAY_ID")
MESSAGES_TOPIC_ARN = os.environ["MESSAGES_TOPIC_ARN"]
AGENTCORE_RUNTIME_VERSION = os.environ["AGENTCORE_RUNTIME_VERSION"]
# ARN of the AgentCore runtime to invoke. Primary source is this env var
# (wired by the CDK stack); the per-message field is kept only as a fallback.
AGENTCORE_RUNTIME_ARN = os.environ.get("AGENTCORE_RUNTIME_ARN")

# AWS clients
from botocore.config import Config as BotoConfig
agentcore_client = boto3.client(
    'bedrock-agentcore',
    region_name=AWS_REGION,
    endpoint_url=f"https://bedrock-agentcore.{AWS_REGION}.amazonaws.com",
    config=BotoConfig(read_timeout=900, connect_timeout=10),
)
sns_client = boto3.client('sns', region_name=AWS_REGION)

# Sequence tracking for token streaming
sequence_number = 0


def send_whatsapp_response(details):
    """Send response to WhatsApp via SNS"""
    # FIFO topic: order per session, unique dedup id to avoid dropping repeats.
    group_id = details.get("data", {}).get("sessionId") or details.get("phone_id") or "default"
    sns_client.publish(
        TopicArn=MESSAGES_TOPIC_ARN,
        Message=json.dumps(details),
        MessageGroupId=group_id,
        MessageDeduplicationId=str(uuid.uuid4()),
    )


def on_llm_new_token(connection_id, user_id, session_id, token, run_id):
    """Send individual token to WebSocket client"""
    if token is None or len(token) == 0:
        return

    global sequence_number
    sequence_number += 1

    send_to_client(
        {
            "type": "text",
            "action": ChatbotAction.LLM_NEW_TOKEN.value,
            "connectionId": connection_id,
            "userId": user_id,
            "timestamp": str(int(round(datetime.now().timestamp()))),
            "data": {
                "sessionId": session_id,
                "token": {
                    "runId": run_id,
                    "sequenceNumber": sequence_number,
                    "value": token,
                },
            },
        }
    )


def invoke_agentcore(payload, session_id, agentcore_runtime_arn, streaming=False):
    """
    Invoke AgentCore Runtime with optional streaming response

    Args:
        payload: Request payload dict
        session_id: Session identifier for conversation continuity
        agentcore_runtime_arn: ARN of the AgentCore Runtime to invoke
        streaming: Whether to stream tokens to the client

    Returns:
        Dict with complete response
    """
    try:
        # Prepare payload for AgentCore
        agentcore_payload = json.dumps(payload).encode('utf-8')
        run_id = str(uuid.uuid4())
        
        logger.info(f"Invoking AgentCore with streaming={streaming}")
        
        # Build invoke parameters
        invoke_params = {
            'agentRuntimeArn': agentcore_runtime_arn,
            'payload': agentcore_payload,
            'qualifier': AGENTCORE_RUNTIME_VERSION,
            'contentType': 'application/json',
        }
        
        # Request streaming format if streaming is enabled
        if streaming:
            invoke_params['accept'] = 'text/event-stream'
        else:
            invoke_params['accept'] = 'application/json'
        
        # Invoke AgentCore Runtime
        response = agentcore_client.invoke_agent_runtime(**invoke_params)

        # Process response
        full_response = ""
        logger.info(f"AgentCore response: {response}")

        # Get the streaming body from response
        streaming_body = response.get('response')

        if streaming_body:
            logger.info(f"Streaming body type: {type(streaming_body)}")
            content_type = response.get('contentType', '')
            logger.info(f"Content type: {content_type}")

            # Always read the full content first
            response_bytes = streaming_body.read()
            response_text = response_bytes.decode('utf-8')
            logger.info(f"Raw response text: {response_text[:500]}...")  # Log first 500 chars

            if streaming:
                # Process as streaming - parse line by line and send tokens
                logger.info("Processing streaming response")
                
                # Check if it's SSE format or NDJSON
                if "text/event-stream" in content_type or response_text.startswith("data:"):
                    # SSE format
                    logger.info("Detected SSE format")
                    for line in response_text.split('\n'):
                        line = line.strip()
                        if not line or line.startswith(':'):
                            continue
                        
                        if line.startswith("data:"):
                            data_content = line[5:].strip()  # Remove "data:" prefix
                        else:
                            data_content = line
                        
                        if not data_content:
                            continue
                            
                        try:
                            chunk_data = json.loads(data_content)
                            
                            # Handle different token formats
                            token = None
                            if 'token' in chunk_data:
                                token = chunk_data['token']
                            elif 'content' in chunk_data:
                                token = chunk_data['content']
                            elif 'delta' in chunk_data:
                                delta = chunk_data['delta']
                                if isinstance(delta, dict):
                                    token = delta.get('text', '')
                                else:
                                    token = str(delta)
                            elif 'text' in chunk_data:
                                token = chunk_data['text']
                            
                            if token:
                                full_response += token
                                on_llm_new_token(
                                    payload.get('connectionId'),
                                    payload.get('userId'),
                                    session_id,
                                    token,
                                    run_id
                                )
                            
                            # Check for final result
                            if 'result' in chunk_data:
                                full_response = chunk_data['result']
                                
                        except json.JSONDecodeError:
                            # Plain text data
                            if data_content:
                                full_response += data_content
                                on_llm_new_token(
                                    payload.get('connectionId'),
                                    payload.get('userId'),
                                    session_id,
                                    data_content,
                                    run_id
                                )
                else:
                    # Try to parse as single JSON first (most common case for AgentCore)
                    logger.info("Trying to parse as JSON")
                    try:
                        response_data = json.loads(response_text)
                        
                        # AgentCore returns {"response": "..."} format
                        extracted_response = None
                        if 'response' in response_data:
                            extracted_response = response_data['response']
                            logger.info(f"Found 'response' field, length: {len(extracted_response)}")
                        elif 'token' in response_data:
                            extracted_response = response_data['token']
                        elif 'result' in response_data:
                            extracted_response = response_data['result']
                        elif 'content' in response_data:
                            extracted_response = response_data['content']
                        elif 'text' in response_data:
                            extracted_response = response_data['text']
                        else:
                            # Unknown structure, use raw text
                            extracted_response = response_text
                        
                        # Simulate streaming by sending words one by one
                        if extracted_response:
                            logger.info("Simulating streaming by sending words")
                            words = extracted_response.split(' ')
                            for i, word in enumerate(words):
                                # Add space back (except for first word)
                                token = word if i == 0 else ' ' + word
                                full_response += token
                                on_llm_new_token(
                                    payload.get('connectionId'),
                                    payload.get('userId'),
                                    session_id,
                                    token,
                                    run_id
                                )
                            
                    except json.JSONDecodeError:
                        # Try NDJSON format (newline-delimited JSON)
                        logger.info("JSON parse failed, trying NDJSON format")
                        for line in response_text.split('\n'):
                            line = line.strip()
                            if not line:
                                continue
                            
                            try:
                                chunk_data = json.loads(line)
                                
                                token = chunk_data.get('response') or chunk_data.get('token') or chunk_data.get('content') or chunk_data.get('text')
                                if token:
                                    full_response += token
                                    on_llm_new_token(
                                        payload.get('connectionId'),
                                        payload.get('userId'),
                                        session_id,
                                        token,
                                        run_id
                                    )
                                
                                if 'result' in chunk_data:
                                    full_response = chunk_data['result']
                                    
                            except json.JSONDecodeError:
                                # Not JSON, treat as plain text
                                full_response += line
                                on_llm_new_token(
                                    payload.get('connectionId'),
                                    payload.get('userId'),
                                    session_id,
                                    line,
                                    run_id
                                )
            else:
                # Non-streaming: parse entire response as single JSON
                logger.info(f"Non-streaming response text: {response_text[:500]}...")

                try:
                    response_data = json.loads(response_text)

                    # AgentCore returns {"response": "..."} format
                    if 'response' in response_data:
                        full_response = response_data['response']
                    elif 'token' in response_data:
                        full_response = response_data['token']
                    elif 'result' in response_data:
                        full_response = response_data['result']
                    elif 'content' in response_data:
                        full_response = response_data['content']
                    elif 'text' in response_data:
                        full_response = response_data['text']
                    elif isinstance(response_data, str):
                        full_response = response_data
                    else:
                        full_response = response_text

                except json.JSONDecodeError:
                    full_response = response_text

        logger.info(f"AgentCore response length: {len(full_response)}")

        return {
            "content": full_response,
            "metadata": {},
            "sessionId": session_id
        }

    except Exception as e:
        logger.error(f"Error invoking AgentCore: {e}", exc_info=True)
        raise


def handle_run(record):
    """Handle WEB run action - invoke AgentCore and send response"""
    connection_id = record["connectionId"]
    user_id = record["userId"]
    data = record["data"]
    
    # Log para debug - ver estructura del evento
    logger.info(f"Record keys: {list(record.keys())}")
    logger.info(f"Data keys: {list(data.keys())}")
    
    agentcore_runtime_arn = AGENTCORE_RUNTIME_ARN or record.get("agentcoreRuntimeArn") or data.get("agentcoreRuntimeArn")
    logger.info(f"Using AgentCore Runtime ARN: {agentcore_runtime_arn}")

    if not agentcore_runtime_arn:
        raise ValueError("AGENTCORE_RUNTIME_ARN env var is not set and no agentcoreRuntimeArn was provided in the event")

    # Extract parameters
    provider = data.get("provider", "bedrock")
    model_name = data.get("modelName", "anthropic.claude-v2")
    user_question = data["text"]
    workspace_id = data.get("workspaceId")
    session_id = data.get("sessionId") or str(uuid.uuid4())
    model_kwargs = data.get("modelKwargs", {})
    streaming = model_kwargs.get("streaming", False)

    logger.info(f"Processing request - Session: {session_id}, Provider: {provider}, Model: {model_name}, Streaming: {streaming}")

    try:
        # Prepare AgentCore payload
        agentcore_payload = {
            "prompt": user_question,
            "provider": provider,
            "modelName": model_name,
            "workspaceId": workspace_id,
            "sessionId": session_id,
            "userId": user_id,
            "connectionId": connection_id,
            **model_kwargs
        }

        # Invoke AgentCore
        response = invoke_agentcore(agentcore_payload, session_id, agentcore_runtime_arn, streaming=streaming)

        # Send final response
        send_to_client(
            {
                "type": "text",
                "action": ChatbotAction.FINAL_RESPONSE.value,
                "connectionId": connection_id,
                "timestamp": str(int(round(datetime.now().timestamp()))),
                "userId": user_id,
                "data": response,
            }
        )

        logger.info("Response sent successfully")

    except Exception as e:
        logger.error(f"Error handling run: {e}", exc_info=True)
        # Send error to client
        send_to_client(
            {
                "type": "text",
                "action": "error",
                "connectionId": connection_id,
                "userId": user_id,
                "timestamp": str(int(round(datetime.now().timestamp()))),
                "data": {
                    "sessionId": session_id,
                    "content": "I apologize, but I encountered an error processing your request. Please try again.",
                    "type": "text"
                },
            }
        )


def handle_whatsapp_run(record):
    """Handle WHATSAPP run action - invoke AgentCore and send via WhatsApp"""
    connection_id = record["connectionId"]  # phone_id for WhatsApp
    user_id = record["userId"]  # from_phone for WhatsApp
    source = record["source"]
    data = record["data"]
    
    agentcore_runtime_arn = AGENTCORE_RUNTIME_ARN or record.get("agentcoreRuntimeArn") or data.get("agentcoreRuntimeArn")
    logger.info(f"Using AgentCore Runtime ARN: {agentcore_runtime_arn}")

    if not agentcore_runtime_arn:
        raise ValueError("AGENTCORE_RUNTIME_ARN env var is not set and no agentcoreRuntimeArn was provided in the event")

    # Extract parameters
    provider = data.get("provider", "bedrock")
    model_name = data.get("modelName", "anthropic.claude-v2")
    user_question = data["text"]
    workspace_id = data.get("workspaceId")
    session_id = data.get("sessionId") or str(uuid.uuid4())

    logger.info(f"Processing WhatsApp request - Session: {session_id}")

    try:
        # Prepare AgentCore payload
        agentcore_payload = {
            "prompt": user_question,
            "provider": provider,
            "modelName": model_name,
            "workspaceId": workspace_id,
            "sessionId": session_id,
            "userId": user_id
        }

        # Invoke AgentCore (no streaming for WhatsApp)
        response = invoke_agentcore(agentcore_payload, session_id, agentcore_runtime_arn)

        # Send via WhatsApp
        whatsapp_response = {
            "type": "text",
            "direction": "WHATSAPP_OUT",
            "source": source,
            "phone_id": connection_id,
            "from_phone": user_id,
            "timestamp": str(int(round(datetime.now().timestamp()))),
            "data": response,
        }

        send_whatsapp_response(whatsapp_response)
        logger.info("WhatsApp response sent successfully")

    except Exception as e:
        logger.error(f"Error handling WhatsApp run: {e}", exc_info=True)


def handle_heartbeat(record):
    """Handle heartbeat action - keep connection alive"""
    connection_id = record["connectionId"]
    user_id = record["userId"]

    send_to_client(
        {
            "type": "text",
            "action": ChatbotAction.HEARTBEAT.value,
            "connectionId": connection_id,
            "timestamp": str(int(round(datetime.now().timestamp()))),
            "userId": user_id,
            "direction": "OUT"
        }
    )


@tracer.capture_method
def record_handler(record: SQSRecord):
    try:
        """Process individual SQS record"""
        payload: str = record.body
        message: dict = json.loads(payload)
        detail: dict = json.loads(message["Message"])
        logger.info(detail)

        # Route based on action and source
        if detail["action"] == ChatbotAction.RUN.value:
            if detail["source"] == "WEB":
                handle_run(detail)
            elif detail["source"] == "WHATSAPP":
                handle_whatsapp_run(detail)

            elif detail["action"] == ChatbotAction.HEARTBEAT.value:
                handle_heartbeat(detail)
    except Exception as e:
        logger.error(f"Error processing record: {e}", exc_info=True)
        raise



def handle_failed_records(records):
    """Handle batch processing failures"""
    for triplet in records:
        status, error, record = triplet
        payload: str = record.body
        message: dict = json.loads(payload)
        detail: dict = json.loads(message["Message"])

        connection_id = detail.get("connectionId")
        user_id = detail.get("userId")
        session_id = detail.get("data", {}).get("sessionId", "")

        send_to_client(
            {
                "type": "text",
                "action": "error",
                "direction": "OUT",
                "connectionId": connection_id,
                "userId": user_id,
                "timestamp": str(int(round(datetime.now().timestamp()))),
                "data": {
                    "sessionId": session_id,
                    "content": str(error),
                    "type": "text",
                },
            }
        )


@logger.inject_lambda_context(log_event=True)
@tracer.capture_lambda_handler
def handler(event, context: LambdaContext):
    """Lambda handler - process SQS batch"""
    batch = event["Records"]

    # Process batch
    try:
        with processor(records=batch, handler=record_handler):
            processed_messages = processor.process()
    except BatchProcessingError as e:
        logger.error(e)

    logger.info(f"Processed {len(processed_messages)} messages")

    # Handle failures
    failed_records = [msg for msg in processed_messages if msg[0] == "fail"]
    if failed_records:
        handle_failed_records(failed_records)

    return processor.response()
