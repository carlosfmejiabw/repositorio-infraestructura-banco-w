import aws_cdk as cdk
from aws_cdk import (
    Stack, RemovalPolicy, Duration,
    aws_s3 as s3,
    aws_lambda as lambda_,
    aws_apigateway as apigw,
    aws_iam as iam,
    aws_dynamodb as dynamodb,
)
from constructs import Construct

POWERTOOLS_LAYER_ARN = (
    "arn:aws:lambda:us-east-1:017000801446:layer:AWSLambdaPowertoolsPythonV2:78"
)


class DocumentsWorkflowStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        prompts_table: dynamodb.Table,
        **kwargs,
    ):
        super().__init__(scope, construct_id, **kwargs)

        # ── S3: Documents bucket ─────────────────────────────────────────────
        documents_bucket = s3.Bucket(
            self, "DocumentsBucket",
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ── S3 Vectors: Bucket + Index ───────────────────────────────────────
        from aws_cdk import aws_s3vectors as s3vectors

        vector_bucket = s3vectors.CfnVectorBucket(
            self, "VectorBucket",
            vector_bucket_name="ofid-vectors",
        )

        vector_index = s3vectors.CfnIndex(
            self, "VectorIndex",
            vector_bucket_name=vector_bucket.vector_bucket_name,
            index_name="ofid-kb-index",
            dimension=1024,
            distance_metric="cosine",
            data_type="float32",
        )
        vector_index.add_dependency(vector_bucket)

        # ── IAM Role for Bedrock Knowledge Base ─────────────────────────────
        kb_role = iam.Role(
            self, "KnowledgeBaseRole",
            role_name="ofid-knowledge-base-role",
            assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"),
            inline_policies={
                "BedrockKBPolicy": iam.PolicyDocument(statements=[
                    iam.PolicyStatement(actions=["bedrock:InvokeModel"], resources=["*"]),
                    iam.PolicyStatement(
                        actions=["s3:GetObject", "s3:ListBucket"],
                        resources=[documents_bucket.bucket_arn, f"{documents_bucket.bucket_arn}/*"],
                    ),
                    iam.PolicyStatement(actions=["s3vectors:*"], resources=["*"]),
                ])
            },
        )

        # ── Bedrock Knowledge Base ───────────────────────────────────────────
        from aws_cdk import aws_bedrock as bedrock

        knowledge_base = bedrock.CfnKnowledgeBase(
            self, "KnowledgeBase",
            name="ofid-knowledge-base",
            role_arn=kb_role.role_arn,
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="VECTOR",
                vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                    embedding_model_arn=f"arn:aws:bedrock:{self.region}::foundation-model/amazon.titan-embed-text-v2:0",
                    embedding_model_configuration=bedrock.CfnKnowledgeBase.EmbeddingModelConfigurationProperty(
                        bedrock_embedding_model_configuration=bedrock.CfnKnowledgeBase.BedrockEmbeddingModelConfigurationProperty(
                            dimensions=1024,
                        )
                    ),
                ),
            ),
            storage_configuration=bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                type="S3_VECTORS",
                s3_vectors_configuration=bedrock.CfnKnowledgeBase.S3VectorsConfigurationProperty(
                    index_arn=vector_index.attr_index_arn,
                ),
            ),
        )
        knowledge_base.add_dependency(vector_index)
        knowledge_base.node.add_dependency(kb_role)

        # ── Bedrock Data Source ──────────────────────────────────────────────
        bedrock.CfnDataSource(
            self, "DocumentsDataSource",
            name="ofid-documents-datasource",
            knowledge_base_id=knowledge_base.attr_knowledge_base_id,
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="S3",
                s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                    bucket_arn=documents_bucket.bucket_arn,
                ),
            ),
        )

        # ── Lambda: prompt-handler ───────────────────────────────────────────
        powertools_layer = lambda_.LayerVersion.from_layer_version_arn(
            self, "PowertoolsLayer", POWERTOOLS_LAYER_ARN
        )
        prompt_handler = lambda_.Function(
            self, "PromptHandler",
            function_name="ofid-prompt-handler",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="index.handler",
            code=lambda_.Code.from_asset("lambdas/prompt-handler"),
            memory_size=512,
            timeout=Duration.minutes(10),
            layers=[powertools_layer],
            environment={"PROMPT_TABLE_NAME": prompts_table.table_name},
        )
        prompts_table.grant_full_access(prompt_handler)

        # ── REST API Gateway ─────────────────────────────────────────────────
        api = apigw.RestApi(
            self, "DocumentsApi",
            rest_api_name="ofid-documents-api",
            deploy_options=apigw.StageOptions(stage_name="api"),
            binary_media_types=["*/*"],
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=apigw.Cors.ALL_METHODS,
            ),
        )

        # /v1/{proxy+} → prompt-handler Lambda
        v1 = api.root.add_resource("v1")
        proxy = v1.add_resource("{proxy+}")
        lambda_integration = apigw.LambdaIntegration(prompt_handler, proxy=True)
        for method in ["GET", "POST", "PUT", "DELETE", "PATCH"]:
            proxy.add_method(method, lambda_integration)

        # /{bucket}/{filename} PUT → S3 direct integration
        s3_role = iam.Role(
            self, "ApiGwS3Role",
            assumed_by=iam.ServicePrincipal("apigateway.amazonaws.com"),
        )
        s3_role.add_to_policy(iam.PolicyStatement(
            actions=["s3:PutObject"],
            resources=[f"{documents_bucket.bucket_arn}/*"],
        ))

        bucket_resource = api.root.add_resource("{bucket}")
        filename_resource = bucket_resource.add_resource("{filename}")
        filename_resource.add_method(
            "PUT",
            apigw.AwsIntegration(
                service="s3",
                integration_http_method="PUT",
                path="{bucket}/{filename}",
                options=apigw.IntegrationOptions(
                    credentials_role=s3_role,
                    request_parameters={
                        "integration.request.path.bucket": "method.request.path.bucket",
                        "integration.request.path.filename": "method.request.path.filename",
                    },
                    integration_responses=[apigw.IntegrationResponse(status_code="200")],
                ),
            ),
            method_responses=[apigw.MethodResponse(status_code="200")],
            request_parameters={
                "method.request.path.bucket": True,
                "method.request.path.filename": True,
            },
        )

        # ── Outputs ───────────────────────────────────────────────────────────
        cdk.CfnOutput(self, "DocumentsBucketName", value=documents_bucket.bucket_name, export_name="ofid-documents-bucket")
        cdk.CfnOutput(self, "DocumentsApiEndpoint", value=api.url, export_name="ofid-documents-api-url")
        cdk.CfnOutput(self, "KnowledgeBaseId", value=knowledge_base.attr_knowledge_base_id, export_name="ofid-kb-id")
