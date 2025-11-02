import os

import aws_cdk as cdk

# Import the new V2 stack
from minecraft_server.minecraft_server_stack import MinecraftServerStack

# Or keep the old stack if you prefer
# from minecraft_server.minecraft_server_stack import MinecraftServerStack

app = cdk.App()

# Get AWS account and region from CDK context or environment variables
# Priority: CDK context > Environment variables > CDK defaults
account = app.node.try_get_context('account') or \
          os.getenv('CDK_DEPLOY_ACCOUNT') or \
          os.getenv('CDK_DEFAULT_ACCOUNT')

region = app.node.try_get_context('region') or \
         os.getenv('CDK_DEPLOY_REGION') or \
         os.getenv('CDK_DEFAULT_REGION') or \
         'us-east-1'

prefix = app.node.try_get_context('bucket_name_prefix') or \
        os.getenv('BUCKET_NAME_PREFIX')

# Deploy the new cost-optimized stack
MinecraftServerStack(app, f"{prefix}MinecraftServer",
    # Configure environment with account and region
    # You can set these via:
    # 1. Environment variables: CDK_DEPLOY_ACCOUNT, CDK_DEPLOY_REGION
    # 2. CDK context in cdk.json or cdk.context.json
    # 3. AWS CLI defaults (CDK_DEFAULT_ACCOUNT, CDK_DEFAULT_REGION)
    env=cdk.Environment(account=account, region=region),

    # For more information, see https://docs.aws.amazon.com/cdk/latest/guide/environments.html
)

app.synth()
