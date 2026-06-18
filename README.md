# OFID — Infraestructura CDK (Banco W)

**Proyecto:** OFID — Oficina Inteligente de Datos  
**Cliente:** Banco W  
**Región:** `us-east-1`  
**Runtime CDK:** Python 3.11 · aws-cdk-lib ≥ 2.260

Infraestructura como código (CDK) del asistente conversacional GenAI con RAG que permite consultar en lenguaje natural el catálogo de datos, glosario, pipelines e inventario de tableros de Banco W.

---

## Arquitectura — Stacks

```
ChatbotWorkflowStack          DocumentsWorkflowStack        FrontendStack
─────────────────────         ──────────────────────        ─────────────
WebSocket API GW              S3 Documents Bucket           S3 Static Site (OAC)
  ├─ $connect                 S3 Vectors Bucket             CloudFront Distribution
  ├─ $disconnect              Bedrock Knowledge Base          ├─ WAF Web ACL (5 reglas)
  └─ $default                   └─ Data Source (S3)           ├─ OAC
Lambda connection-handler     Lambda prompt-handler           └─ SPA fallback 403/404
Lambda incoming-handler       REST API Gateway
Lambda outgoing-handler         ├─ /v1/{proxy+} → Lambda
Lambda request-handler          └─ /{bucket}/{filename} PUT → S3
SNS message-topic
SQS outgoing + langchain
  └─ DLQs
DynamoDB Connections/Sessions/Prompts
```

---

## Prerequisitos

- Python 3.11+
- Node.js 18+ (requerido por CDK CLI)
- AWS CDK CLI: `npm install -g aws-cdk`
- AWS CLI configurado con credenciales para la cuenta destino
- Cuenta AWS boostrapeada: `cdk bootstrap aws://<ACCOUNT_ID>/us-east-1`

---

## Instalación

```bash
cd repositorio-infraestructura-banco-w
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Mac/Linux
pip install -r requirements.txt
```

---

## Estructura del proyecto

```
repositorio-infraestructura-banco-w/
├── app.py                                          # Entry point CDK
├── requirements.txt
├── lambdas/
│   ├── wss-connection-handler/index.py
│   ├── wss-incoming-message-handler/index.py
│   ├── wss-outgoing-message-handler/index.py
│   ├── request-handler/index.py
│   │   └── genai_core/                             # Módulo compartido
│   └── prompt-handler/                             # ⚠ Crear antes de desplegar
│       └── index.py
└── repositorio_infraestructura_banco_w/
    ├── chatbot_workflow_stack.py
    ├── documents_workflow_stack.py
    └── frontend_stack.py
```

> **Nota:** La carpeta `lambdas/prompt-handler/` debe crearse manualmente con el código de la Lambda `prompt-handler` descrito en la guía `02-chatbot-lambdas.md` antes de ejecutar `cdk deploy`.

---

## Variables a completar antes del despliegue

| Variable / Placeholder | Dónde | Descripción |
|---|---|---|
| `AGENTCORE_RUNTIME_ARN` | `chatbot_workflow_stack.py` → env `request-handler` | ARN del AgentCore Runtime desplegado |
| `<ACCOUNT_ID>` | Comando `cdk bootstrap` | ID de la cuenta AWS (12 dígitos) |
| Certificado ACM | `frontend_stack.py` (opcional) | ARN del certificado TLS para dominio custom |
| Dominio custom | `frontend_stack.py` (opcional) | CNAME para la distribución CloudFront |

---

## Comandos CDK

```bash
# Ver los stacks disponibles
cdk ls

# Sintetizar CloudFormation (sin desplegar)
cdk synth

# Desplegar un stack específico
cdk deploy ChatbotWorkflowStack
cdk deploy DocumentsWorkflowStack
cdk deploy FrontendStack

# Desplegar todos los stacks
cdk deploy --all

# Destruir (¡precaución en producción!)
cdk destroy --all
```

### Orden recomendado de despliegue

1. `ChatbotWorkflowStack` — tablas, colas y WebSocket API
2. `DocumentsWorkflowStack` — Knowledge Base y API de documentos
3. `FrontendStack` — WAF + CloudFront + S3

---

## Outputs importantes

| Stack | Output | Descripción |
|---|---|---|
| ChatbotWorkflowStack | `WebSocketApiEndpoint` | URL WebSocket `wss://…/socket` |
| ChatbotWorkflowStack | `MessageTopicArn` | ARN del SNS topic |
| DocumentsWorkflowStack | `KnowledgeBaseId` | ID de la Bedrock Knowledge Base |
| DocumentsWorkflowStack | `DocumentsApiEndpoint` | URL del REST API Gateway |
| FrontendStack | `CloudFrontURL` | URL pública del frontend |
| FrontendStack | `DistributionId` | ID de la distribución CloudFront |

---

## Sincronización de la Knowledge Base

Tras subir documentos al bucket `ofid-upload-documents-<account>`:

```bash
aws bedrock start-ingestion-job \
  --knowledge-base-id <KnowledgeBaseId> \
  --data-source-id <DataSourceId> \
  --region us-east-1
```

---

## Despliegue del frontend

```bash
# Desde el directorio del build del frontend
aws s3 sync ./dist s3://ofid-frontend-<account> --delete

# Invalidar caché de CloudFront
aws cloudfront create-invalidation \
  --distribution-id <DistributionId> \
  --paths "/*"
```

---

## Seguridad y trazabilidad

Los siguientes servicios se configuran manualmente fuera del IaC (transversales a todos los stacks):

- **AWS Budgets** — alertas de costo
- **CloudTrail** — auditoría de llamadas a la API
- **IAM Roles/Permissions** — principio de mínimo privilegio
- **Bedrock Guardrails** — filtros de contenido para el modelo
- **CloudWatch** — métricas, logs y alarmas

---

## Repositorios relacionados

| Repositorio | Descripción |
|---|---|
| `repositorio-infraestructura-banco-w` | Este repositorio — IaC CDK |
| `repositorio-agentcor-banco-w` | Agente AgentCore + servidor MCP (Power BI) |
| `repositorio-frontend-banco-w` | Aplicación web React del asistente |
