import aws_cdk as cdk
from aws_cdk import (
    Stack, Duration, RemovalPolicy,
    aws_dynamodb as dynamodb,
    aws_sns as sns,
    aws_sns_subscriptions as sns_subs,
    aws_sqs as sqs,
    aws_lambda as lambda_,
    aws_lambda_event_sources as lambda_events,
    aws_apigatewayv2 as apigwv2,
    aws_iam as iam,
)
from aws_cdk.aws_apigatewayv2_integrations import WebSocketLambdaIntegration
from constructs import Construct

POWERTOOLS_LAYER_ARN = (
    "arn:aws:lambda:us-east-1:017000801446:layer:AWSLambdaPowertoolsPythonV2:78"
)


class ChatbotWorkflowStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        # ── DynamoDB Tables ─────────────────────────────────────────────────
        self.connections_table = dynamodb.Table(
            self, "ConnectionsTable",
            table_name="ofid-connections",
            partition_key=dynamodb.Attribute(name="connectionId", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.connections_table.add_global_secondary_index(
            index_name="byUser",
            partition_key=dynamodb.Attribute(name="userId", type=dynamodb.AttributeType.STRING),
        )

        self.prompts_table = dynamodb.Table(
            self, "PromptsTable",
            table_name="ofid-prompts",
            partition_key=dynamodb.Attribute(name="Id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.sessions_table = dynamodb.Table(
            self, "SessionsTable",
            table_name="ofid-sessions",
            partition_key=dynamodb.Attribute(name="SessionId", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="UserId", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.sessions_table.add_global_secondary_index(
            index_name="byUserId",
            partition_key=dynamodb.Attribute(name="UserId", type=dynamodb.AttributeType.STRING),
        )

        # ── SNS Topic ────────────────────────────────────────────────────────
        self.message_topic = sns.Topic(self, "MessageTopic", topic_name="ofid-message-topic")

        # ── SQS Queues ───────────────────────────────────────────────────────
        dlq_outgoing = sqs.Queue(
            self, "DlqOutgoing",
            queue_name="ofid-dlq-outgoing-message",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
        )
        self.queue_outgoing = sqs.Queue(
            self, "QueueOutgoing",
            queue_name="ofid-queue-outgoing-message",
            visibility_timeout=Duration.minutes(1),
            retention_period=Duration.days(4),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=dlq_outgoing),
        )

        dlq_langchain = sqs.Queue(
            self, "DlqLangchain",
            queue_name="ofid-dlq-langchain-ingestion",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
        )
        self.queue_langchain = sqs.Queue(
            self, "QueueLangchain",
            queue_name="ofid-queue-langchain-ingestion",
            visibility_timeout=Duration.minutes(90),
            retention_period=Duration.days(4),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=dlq_langchain),
        )

        # SNS → SQS subscriptions with filter policies
        self.message_topic.add_subscription(
            sns_subs.SqsSubscription(
                self.queue_outgoing,
                raw_message_delivery=False,
                filter_policy_with_message_body={
                    "direction": sns.FilterOrPolicy.filter(
                        sns.SubscriptionFilter.string_filter(allowlist=["OUT"])
                    )
                },
            )
        )
        self.message_topic.add_subscription(
            sns_subs.SqsSubscription(
                self.queue_langchain,
                raw_message_delivery=False,
                filter_policy_with_message_body={
                    "modelInterface": sns.FilterOrPolicy.filter(
                        sns.SubscriptionFilter.string_filter(allowlist=["langchain"])
                    ),
                    "direction": sns.FilterOrPolicy.filter(
                        sns.SubscriptionFilter.string_filter(allowlist=["IN"])
                    ),
                },
            )
        )

        # ── Lambda Layer ─────────────────────────────────────────────────────
        powertools_layer = lambda_.LayerVersion.from_layer_version_arn(
            self, "PowertoolsLayer", POWERTOOLS_LAYER_ARN
        )

        # ── Lambda: connection-handler ───────────────────────────────────────
        connection_handler = lambda_.Function(
            self, "ConnectionHandler",
            function_name="ofid-wss-connection-handler",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="index.handler",
            code=lambda_.Code.from_asset("lambdas/wss-connection-handler"),
            memory_size=128,
            timeout=Duration.seconds(10),
            layers=[powertools_layer],
            environment={"CONNECTIONS_TABLE_NAME": self.connections_table.table_name},
        )
        self.connections_table.grant_full_access(connection_handler)
        connection_handler.add_to_role_policy(
            iam.PolicyStatement(actions=["logs:*"], resources=["*"])
        )

        # ── Lambda: incoming-message-handler ────────────────────────────────
        incoming_handler = lambda_.Function(
            self, "IncomingHandler",
            function_name="ofid-wss-incoming-message-handler",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="index.handler",
            code=lambda_.Code.from_asset("lambdas/wss-incoming-message-handler"),
            memory_size=256,
            timeout=Duration.minutes(1),
            layers=[powertools_layer],
            environment={
                "MESSAGES_TOPIC_ARN": self.message_topic.topic_arn,
                "WEBSOCKET_API_ENDPOINT": "",  # updated after API creation below
            },
        )
        self.message_topic.grant_publish(incoming_handler)
        incoming_handler.add_to_role_policy(
            iam.PolicyStatement(actions=["execute-api:ManageConnections"], resources=["*"])
        )

        # ── Lambda: outgoing-message-handler ────────────────────────────────
        outgoing_handler = lambda_.Function(
            self, "OutgoingHandler",
            function_name="ofid-wss-outgoing-message-handler",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="index.handler",
            code=lambda_.Code.from_asset("lambdas/wss-outgoing-message-handler"),
            memory_size=256,
            timeout=Duration.minutes(1),
            layers=[powertools_layer],
            environment={
                "CONNECTIONS_TABLE_NAME": self.connections_table.table_name,
                "WEBSOCKET_API_ENDPOINT": "",  # updated after API creation below
            },
        )
        self.connections_table.grant_full_access(outgoing_handler)
        self.queue_outgoing.grant_consume_messages(outgoing_handler)
        outgoing_handler.add_to_role_policy(
            iam.PolicyStatement(actions=["execute-api:ManageConnections"], resources=["*"])
        )

        # ── Lambda: request-handler ──────────────────────────────────────────
        request_handler = lambda_.Function(
            self, "RequestHandler",
            function_name="ofid-request-handler",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="index.handler",
            code=lambda_.Code.from_asset("lambdas/request-handler"),
            memory_size=1024,
            timeout=Duration.minutes(15),
            layers=[powertools_layer],
            environment={
                "AGENTCORE_RUNTIME_ARN": "",
                "AGENTCORE_RUNTIME_VERSION": "DRAFT",
                "LOG_LEVEL": "INFO",
                "MESSAGES_TOPIC_ARN": self.message_topic.topic_arn,
                "POWERTOOLS_DEV": "true",
                "POWERTOOLS_LOGGER_LOG_EVENT": "true",
                "POWERTOOLS_SERVICE_NAME": "chatbot",
                "PROMPT_TABLE_NAME": self.prompts_table.table_name,
                "SESSIONS_BY_USER_ID_INDEX_NAME": "byUserId",
                "SESSIONS_TABLE_NAME": self.sessions_table.table_name,
            },
        )
        self.prompts_table.grant_full_access(request_handler)
        self.sessions_table.grant_full_access(request_handler)
        self.message_topic.grant_publish(request_handler)
        self.queue_langchain.grant_consume_messages(request_handler)
        request_handler.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "bedrock:*", "secretsmanager:*", "ssm:*",
                "s3:*", "execute-api:ManageConnections",
            ],
            resources=["*"],
        ))

        # SQS triggers
        outgoing_handler.add_event_source(
            lambda_events.SqsEventSource(self.queue_outgoing, batch_size=10)
        )
        request_handler.add_event_source(
            lambda_events.SqsEventSource(self.queue_langchain, batch_size=1)
        )

        # ── WebSocket API Gateway ────────────────────────────────────────────
        ws_api = apigwv2.WebSocketApi(
            self, "ChatbotWsApi",
            api_name="chatbot-ws-api",
            route_selection_expression="$request.body.action",
            connect_route_options=apigwv2.WebSocketRouteOptions(
                integration=WebSocketLambdaIntegration("ConnectIntegration", connection_handler)
            ),
            disconnect_route_options=apigwv2.WebSocketRouteOptions(
                integration=WebSocketLambdaIntegration("DisconnectIntegration", connection_handler)
            ),
            default_route_options=apigwv2.WebSocketRouteOptions(
                integration=WebSocketLambdaIntegration("DefaultIntegration", incoming_handler)
            ),
        )

        ws_stage = apigwv2.WebSocketStage(
            self, "SocketStage",
            web_socket_api=ws_api,
            stage_name="socket",
            auto_deploy=True,
        )

        ws_endpoint = f"https://{ws_api.api_id}.execute-api.{self.region}.amazonaws.com/socket"

        # Patch env vars that need the WebSocket endpoint
        incoming_handler.add_environment("WEBSOCKET_API_ENDPOINT", ws_endpoint)
        outgoing_handler.add_environment("WEBSOCKET_API_ENDPOINT", ws_endpoint)

        # ── Outputs ───────────────────────────────────────────────────────────
        cdk.CfnOutput(self, "WebSocketApiEndpoint", value=ws_stage.url, export_name="ofid-ws-endpoint")
        cdk.CfnOutput(self, "MessageTopicArn", value=self.message_topic.topic_arn, export_name="ofid-message-topic-arn")
