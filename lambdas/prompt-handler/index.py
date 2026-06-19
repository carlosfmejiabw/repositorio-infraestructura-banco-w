# Prompt Handler Lambda - OFID Banco W
# Full implementation in guide 02-chatbot-lambdas.md

import json
from decimal import Decimal
from pydantic import ValidationError
from botocore.exceptions import ClientError
from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.logging import correlation_paths
from aws_lambda_powertools.utilities.typing import LambdaContext
from aws_lambda_powertools.event_handler.api_gateway import Response
from aws_lambda_powertools.event_handler import (
    APIGatewayRestResolver,
    CORSConfig,
    content_types,
)
from routes.prompts import router as prompts_router

logger = Logger()
tracer = Tracer()


class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


cors_config = CORSConfig(allow_origin="*", max_age=0)
app = APIGatewayRestResolver(
    cors=cors_config,
    strip_prefixes=["/v1"],
    serializer=lambda obj: json.dumps(obj, cls=CustomJSONEncoder),
)
app.include_router(prompts_router)


@app.exception_handler(ClientError)
def handle_client_error(e: ClientError):
    logger.exception(e)
    return Response(
        status_code=200,
        content_type=content_types.APPLICATION_JSON,
        body=json.dumps({"ok": False, "data": str(e)}, cls=CustomJSONEncoder),
    )


@app.exception_handler(ValidationError)
def handle_validation_error(e: ValidationError):
    logger.exception(e)
    return Response(
        status_code=200,
        content_type=content_types.APPLICATION_JSON,
        body=json.dumps(
            {"ok": False, "data": [str(error) for error in e.errors()]},
            cls=CustomJSONEncoder,
        ),
    )


@logger.inject_lambda_context(
    log_event=False, correlation_id_path=correlation_paths.API_GATEWAY_REST
)
@tracer.capture_lambda_handler
def handler(event: dict, context: LambdaContext) -> dict:
    return app.resolve(event, context)
