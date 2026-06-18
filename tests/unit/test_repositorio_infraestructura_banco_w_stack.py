import aws_cdk as core
import aws_cdk.assertions as assertions

from repositorio_infraestructura_banco_w.repositorio_infraestructura_banco_w_stack import RepositorioInfraestructuraBancoWStack

# example tests. To run these tests, uncomment this file along with the example
# resource in repositorio_infraestructura_banco_w/repositorio_infraestructura_banco_w_stack.py
def test_sqs_queue_created():
    app = core.App()
    stack = RepositorioInfraestructuraBancoWStack(app, "repositorio-infraestructura-banco-w")
    template = assertions.Template.from_stack(stack)

#     template.has_resource_properties("AWS::SQS::Queue", {
#         "VisibilityTimeout": 300
#     })
