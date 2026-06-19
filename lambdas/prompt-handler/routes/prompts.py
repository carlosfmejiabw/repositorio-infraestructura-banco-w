from pydantic import BaseModel
from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.event_handler.api_gateway import Router
import os
import boto3
import uuid
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

dynamodb = boto3.resource("dynamodb")
tracer = Tracer()
router = Router()
logger = Logger()


def get_table():
    table_name = os.environ.get("PROMPT_TABLE_NAME")
    if not table_name:
        raise ValueError("PROMPT_TABLE_NAME environment variable is required")
    return dynamodb.Table(table_name.strip())


class NewPromptRequest(BaseModel):
    name: str
    prompt: str
    is_default: bool = False


class SetDefaultPromptRequest(BaseModel):
    prompt_id: str


def _get_item_by_id(id: str):
    return get_table().get_item(Key={"Id": id})


@router.get("/prompts")
@tracer.capture_method
def get_prompts():
    client = boto3.client("dynamodb")
    deserializer = TypeDeserializer()
    table_name = os.environ.get("PROMPT_TABLE_NAME", "").strip()
    all_items, last_key = [], None
    try:
        while True:
            params = {"TableName": table_name}
            if last_key:
                params["ExclusiveStartKey"] = last_key
            response = client.scan(**params)
            all_items.extend(response["Items"])
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
        return {"ok": True, "data": [{k: deserializer.deserialize(v) for k, v in item.items()} for item in all_items]}
    except Exception as e:
        return {"ok": False, "data": str(e)}


@router.get("/prompt/default")
@tracer.capture_method
def get_default_prompt():
    try:
        client = boto3.client("dynamodb")
        deserializer = TypeDeserializer()
        table_name = os.environ.get("PROMPT_TABLE_NAME", "").strip()
        response = client.scan(
            TableName=table_name,
            FilterExpression="is_default = :val",
            ExpressionAttributeValues={":val": {"BOOL": True}},
        )
        if not response["Items"]:
            return {"ok": True, "data": None}
        return {"ok": True, "data": {k: deserializer.deserialize(v) for k, v in response["Items"][0].items()}}
    except Exception as e:
        return {"ok": False, "data": str(e)}


@router.post("/prompt/default")
@tracer.capture_method
def set_default_prompt():
    try:
        data = router.current_event.json_body
        request = SetDefaultPromptRequest(**data)
        table = get_table()
        prompt = _get_item_by_id(request.prompt_id)
        if not prompt.get("Item"):
            return {"ok": False, "data": "Prompt not found"}
        client = boto3.client("dynamodb")
        table_name = os.environ.get("PROMPT_TABLE_NAME", "").strip()
        for item in client.scan(TableName=table_name)["Items"]:
            table.update_item(
                Key={"Id": item["Id"]["S"]},
                UpdateExpression="SET is_default = :val",
                ExpressionAttributeValues={":val": False},
            )
        table.update_item(
            Key={"Id": request.prompt_id},
            UpdateExpression="SET is_default = :val",
            ExpressionAttributeValues={":val": True},
            ReturnValues="ALL_NEW",
        )
        return {"ok": True, "data": prompt.get("Item")}
    except Exception as e:
        return {"ok": False, "data": str(e)}


@router.get("/prompt/<prompt_id>")
@tracer.capture_method
def get_prompt(prompt_id: str):
    try:
        item = _get_item_by_id(prompt_id).get("Item")
        return {"ok": True, "data": item} if item else {"ok": False, "data": "Prompt does not exist"}
    except Exception as e:
        return {"ok": False, "data": str(e)}


@router.post("/prompt")
@tracer.capture_method
def create_prompt():
    data = router.current_event.json_body
    request = NewPromptRequest(**data)
    table = get_table()
    prompt_id = str(uuid.uuid4())
    if request.is_default:
        client = boto3.client("dynamodb")
        table_name = os.environ.get("PROMPT_TABLE_NAME", "").strip()
        for item in client.scan(TableName=table_name)["Items"]:
            table.update_item(
                Key={"Id": item["Id"]["S"]},
                UpdateExpression="SET is_default = :val",
                ExpressionAttributeValues={":val": False},
            )
    table.put_item(Item={"Id": prompt_id, "name": request.name.lower(), "prompt": request.prompt, "is_default": request.is_default})
    return {"ok": True, "Id": prompt_id}


@router.put("/prompt/<prompt_id>")
@tracer.capture_method
def update_prompt(prompt_id: str):
    data = router.current_event.json_body
    request = NewPromptRequest(**data)
    try:
        table = get_table()
        item = _get_item_by_id(prompt_id).get("Item")
        if not item:
            return {"ok": False, "data": "Prompt ID does not exist"}
        item["name"] = request.name.lower()
        item["prompt"] = request.prompt
        table.put_item(Item=item)
        return {"ok": True}
    except ClientError as e:
        return {"ok": False, "data": str(e)}


@router.delete("/prompt/<prompt_id>")
@tracer.capture_method
def delete_prompt(prompt_id: str):
    try:
        table = get_table()
        if _get_item_by_id(prompt_id).get("Item"):
            table.delete_item(Key={"Id": prompt_id})
            return {"ok": True}
        return {"ok": False, "data": "Prompt ID does not exist"}
    except ClientError as e:
        return {"ok": False, "data": str(e)}
