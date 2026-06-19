import json
import os
import uuid

import boto3
from ..types import Direction

sns = boto3.client("sns")


def send_to_client(detail, topic_arn=None):
    if not "direction" in detail:
        detail["direction"] = Direction.OUT.value

    if not topic_arn:
        topic_arn = os.environ["MESSAGES_TOPIC_ARN"]

    # FIFO topic: keep all messages of a session ordered (tokens stream in order),
    # with a unique dedup id so repeated tokens/responses are never dropped.
    group_id = detail.get("data", {}).get("sessionId") or detail.get("connectionId") or "default"

    sns.publish(
        TopicArn=topic_arn,
        Message=json.dumps(detail),
        MessageGroupId=group_id,
        MessageDeduplicationId=str(uuid.uuid4()),
    )
