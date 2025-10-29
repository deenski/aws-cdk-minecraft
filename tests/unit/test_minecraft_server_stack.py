import aws_cdk as core
import aws_cdk.assertions as assertions

from minecraft_server.minecraft_server_stack import MinecraftServerStack

# example tests. To run these tests, uncomment this file along with the example
# resource in minecraft_server/minecraft_server_stack.py
def test_sqs_queue_created():
    app = core.App()
    stack = MinecraftServerStack(app, "minecraft-server")
    template = assertions.Template.from_stack(stack)

#     template.has_resource_properties("AWS::SQS::Queue", {
#         "VisibilityTimeout": 300
#     })
