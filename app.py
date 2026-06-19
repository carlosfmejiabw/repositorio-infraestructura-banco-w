#!/usr/bin/env python3
import aws_cdk as cdk
from repositorio_infraestructura_banco_w.chatbot_workflow_stack import ChatbotWorkflowStack
from repositorio_infraestructura_banco_w.documents_workflow_stack import DocumentsWorkflowStack
from repositorio_infraestructura_banco_w.frontend_stack import FrontendStack

app = cdk.App()
env = cdk.Environment(account=app.node.try_get_context("account"), region="us-east-1")

# ARN of the deployed AgentCore runtime (agentcore repo is deployed first).
# Pass at deploy time: cdk deploy -c agentcoreRuntimeArn=arn:aws:bedrock-agentcore:...
# or set it under "context" in cdk.json.
agentcore_runtime_arn = app.node.try_get_context("agentcoreRuntimeArn")

chatbot_stack = ChatbotWorkflowStack(
    app, "ChatbotWorkflowStack",
    agentcore_runtime_arn=agentcore_runtime_arn,
    env=env,
)
documents_stack = DocumentsWorkflowStack(app, "DocumentsWorkflowStack", prompts_table=chatbot_stack.prompts_table, env=env)
FrontendStack(app, "FrontendStack", env=env)

app.synth()
