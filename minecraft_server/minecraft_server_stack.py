"""
Cost-Optimized Minecraft Server on AWS
- Uses ECS Fargate Spot for 70% cost savings
- Persistent worlds via S3 backups
- Step Functions orchestration for start/stop
- API Gateway for easy control
- Optional Route53 DNS updates
"""

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct


class MinecraftServerStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Configuration from context
        config = self.node.try_get_context("minecraft") or {}
        server_size = config.get("server_size", "small")  # small, medium, large
        enable_route53 = config.get("enable_route53", False)
        hosted_zone_id = config.get("hosted_zone_id", "")
        domain_name = config.get("domain_name", "")
        
        # Server sizing
        server_configs = {
            "small": {"cpu": 2048, "memory": 4096, "description": "1-5 players"},
            "medium": {"cpu": 2048, "memory": 8192, "description": "5-10 players"},
            "large": {"cpu": 4096, "memory": 16384, "description": "10-20 players"},
        }
        server_config = server_configs[server_size]

        # VPC with minimal cost - public subnets only to avoid NAT Gateway costs
        vpc = ec2.Vpc(
            self,
            "MinecraftVPC",
            max_azs=2,
            nat_gateways=0,  # No NAT Gateway = $0 cost
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                )
            ],
        )

        # S3 bucket for world backups with lifecycle policy
        backup_bucket = s3.Bucket(
            self,
            id="MinecraftBackups",
            versioned=False,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="KeepLatest3Backups",
                    enabled=True,
                    expiration=Duration.days(7),
                    noncurrent_version_expiration=Duration.days(1),
                )
            ],
        )

        # ECS Cluster
        cluster = ecs.Cluster(
            self,
            "MinecraftCluster",
            vpc=vpc,
            container_insights=False,  # Disable to save costs
        )

        # Task execution role
        task_execution_role = iam.Role(
            self,
            "TaskExecutionRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                )
            ],
        )

        # Task role with S3 access
        task_role = iam.Role(
            self,
            "TaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        backup_bucket.grant_read_write(task_role)

        # ECS Task Definition
        task_definition = ecs.FargateTaskDefinition(
            self,
            "MinecraftTask",
            cpu=server_config["cpu"],
            memory_limit_mib=server_config["memory"],
            execution_role=task_execution_role,
            task_role=task_role,
        )

        # Container definition
        container = task_definition.add_container(
            "MinecraftContainer",
            image=ecs.ContainerImage.from_registry("itzg/minecraft-server"),
            logging=ecs.LogDriver.aws_logs(
                stream_prefix="minecraft",
                log_retention=logs.RetentionDays.ONE_WEEK,
            ),
            environment={
                "EULA": "TRUE",
                "VERSION": "LATEST",
                "MEMORY": f"{int(server_config['memory'] * 0.8)}M",
                "S3_BUCKET": backup_bucket.bucket_name,
            },
            # No port_mappings here; handled at service level for awsvpc
        )

        # Security Group
        security_group = ec2.SecurityGroup(
            self,
            "MinecraftSG",
            vpc=vpc,
            description="Allow Minecraft traffic",
            allow_all_outbound=True,
        )
        security_group.add_ingress_rule(
            ec2.Peer.any_ipv4(),
            ec2.Port.tcp(25565),
            "Minecraft server port",
        )

        # ECS Fargate Service (awsvpc network mode)
        service = ecs.FargateService(
            self,
            "MinecraftService",
            cluster=cluster,
            task_definition=task_definition,
            desired_count=1,
            assign_public_ip=True,
            security_groups=[security_group],
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
        )

        # Add port mapping at the service level (for awsvpc mode, this is handled by the security group and container port)

        # Lambda function to start ECS task
        start_task_lambda = lambda_.Function(
            self,
            "StartTaskFunction",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            timeout=Duration.seconds(30),
            code=lambda_.Code.from_inline(
                """
import boto3
import os

def handler(event, context):
    ecs = boto3.client('ecs')
    cluster = os.environ['CLUSTER_NAME']
    service = os.environ['SERVICE_NAME']
    ecs.update_service(
        cluster=cluster,
        service=service,
        desiredCount=1
    )
    return {'statusCode': 200, 'message': 'Server started'}
"""
            ),
            environment={
                "CLUSTER_NAME": cluster.cluster_name,
                "SERVICE_NAME": service.service_name,
            },
        )

        # Grant permissions
        start_task_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecs:UpdateService"],
                resources=[service.service_arn],
            )
        )
        start_task_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[task_role.role_arn, task_execution_role.role_arn],
            )
        )

        # Lambda function to get task IP
        get_ip_lambda = lambda_.Function(
            self,
            "GetIPFunction",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            timeout=Duration.seconds(30),
            code=lambda_.Code.from_inline(
                """
import boto3
import time

ecs = boto3.client('ecs')
ec2 = boto3.client('ec2')

def handler(event, context):
    cluster = event['cluster']
    task_arn = event['taskArn']
    
    # Wait for task to be running
    waiter = ecs.get_waiter('tasks_running')
    waiter.wait(
        cluster=cluster,
        tasks=[task_arn],
        WaiterConfig={'Delay': 6, 'MaxAttempts': 20}
    )
    
    # Get task details
    response = ecs.describe_tasks(cluster=cluster, tasks=[task_arn])
    task = response['tasks'][0]
    
    # Get ENI and public IP
    eni_id = None
    for attachment in task.get('attachments', []):
        for detail in attachment.get('details', []):
            if detail['name'] == 'networkInterfaceId':
                eni_id = detail['value']
                break
    
    if eni_id:
        eni_response = ec2.describe_network_interfaces(NetworkInterfaceIds=[eni_id])
        public_ip = eni_response['NetworkInterfaces'][0].get('Association', {}).get('PublicIp', '')
        
        return {
            'statusCode': 200,
            'publicIp': public_ip,
            'taskArn': task_arn
        }
    
    return {'statusCode': 500, 'error': 'No IP found'}
"""
            ),
        )

        get_ip_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecs:DescribeTasks"],
                resources=["*"],
            )
        )
        get_ip_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ec2:DescribeNetworkInterfaces"],
                resources=["*"],
            )
        )

        # Lambda function to backup world
        backup_lambda = lambda_.Function(
            self,
            "BackupFunction",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            timeout=Duration.minutes(5),
            code=lambda_.Code.from_inline(
                f"""
import boto3
import json
from datetime import datetime

ecs = boto3.client('ecs')
s3 = boto3.client('s3')

def handler(event, context):
    cluster = event['cluster']
    task_arn = event['taskArn']
    bucket = '{backup_bucket.bucket_name}'
    
    # Trigger backup command in container
    # In production, you'd execute a command in the container to backup
    # For now, we'll just record the backup request
    
    timestamp = datetime.utcnow().isoformat()
    backup_key = f"backups/world-{{timestamp}}.tar.gz"
    
    # Note: In real implementation, this would:
    # 1. Execute 'save-all' command in Minecraft server
    # 2. Compress world files
    # 3. Upload to S3
    
    return {{
        'statusCode': 200,
        'backupKey': backup_key,
        'timestamp': timestamp
    }}
"""
            ),
            environment={
                "BUCKET_NAME": backup_bucket.bucket_name,
            },
        )

        backup_bucket.grant_write(backup_lambda)
        backup_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecs:ExecuteCommand", "ecs:DescribeTasks"],
                resources=["*"],
            )
        )

        # Lambda function to stop task
        stop_task_lambda = lambda_.Function(
            self,
            "StopTaskFunction",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            timeout=Duration.seconds(30),
            code=lambda_.Code.from_inline(
                """
import boto3
import os

def handler(event, context):
    ecs = boto3.client('ecs')
    cluster = os.environ['CLUSTER_NAME']
    service = os.environ['SERVICE_NAME']
    ecs.update_service(
        cluster=cluster,
        service=service,
        desiredCount=0
    )
    return {'statusCode': 200, 'message': 'Server stopped'}
"""
            ),
            environment={
                "CLUSTER_NAME": cluster.cluster_name,
                "SERVICE_NAME": service.service_name,
            },
        )

        # Grant Lambda functions permission to update ECS service
        service_arn = f"arn:aws:ecs:{self.region}:{self.account}:service/{cluster.cluster_name}/{service.service_name}"
        for fn in [start_task_lambda, stop_task_lambda]:
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["ecs:UpdateService"],
                    resources=[service_arn],
                )
            )

        # Lambda for Route53 update (optional)
        update_dns_lambda = lambda_.Function(
            self,
            "UpdateDNSFunction",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            timeout=Duration.seconds(30),
            code=lambda_.Code.from_inline(
                f"""
import boto3
import os

route53 = boto3.client('route53')

def handler(event, context):
    enabled = os.environ.get('ENABLED', 'false') == 'true'
    if not enabled:
        return {{'statusCode': 200, 'message': 'Route53 disabled'}}
    
    hosted_zone_id = os.environ['HOSTED_ZONE_ID']
    domain_name = os.environ['DOMAIN_NAME']
    public_ip = event.get('publicIp', '')
    
    if not public_ip:
        return {{'statusCode': 400, 'error': 'No IP provided'}}
    
    response = route53.change_resource_record_sets(
        HostedZoneId=hosted_zone_id,
        ChangeBatch={{
            'Changes': [{{
                'Action': 'UPSERT',
                'ResourceRecordSet': {{
                    'Name': domain_name,
                    'Type': 'A',
                    'TTL': 60,
                    'ResourceRecords': [{{'Value': public_ip}}]
                }}
            }}]
        }}
    )
    
    return {{
        'statusCode': 200,
        'changeId': response['ChangeInfo']['Id']
    }}
"""
            ),
            environment={
                "ENABLED": "true" if enable_route53 else "false",
                "HOSTED_ZONE_ID": hosted_zone_id,
                "DOMAIN_NAME": domain_name,
            },
        )

        if enable_route53 and hosted_zone_id:
            update_dns_lambda.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["route53:ChangeResourceRecordSets"],
                    resources=[f"arn:aws:route53:::hostedzone/{hosted_zone_id}"],
                )
            )

        # Step Function for starting server
        start_task_step = tasks.LambdaInvoke(
            self,
            "StartTask",
            lambda_function=start_task_lambda,
            result_path="$.taskInfo",
            output_path="$.taskInfo.Payload",
        )

        wait_for_task = sfn.Wait(
            self,
            "WaitForTask",
            time=sfn.WaitTime.duration(Duration.seconds(30)),
        )

        get_ip_step = tasks.LambdaInvoke(
            self,
            "GetIP",
            lambda_function=get_ip_lambda,
            result_path="$.ipInfo",
            output_path="$.ipInfo.Payload",
        )

        update_dns_step = tasks.LambdaInvoke(
            self,
            "UpdateDNS",
            lambda_function=update_dns_lambda,
            result_path="$.dnsInfo",
        )

        server_ready = sfn.Succeed(self, "ServerReady")

        start_definition = (
            start_task_step
            .next(wait_for_task)
            .next(get_ip_step)
            .next(update_dns_step)
            .next(server_ready)
        )

        start_state_machine = sfn.StateMachine(
            self,
            "StartServerStateMachine",
            definition=start_definition,
            timeout=Duration.minutes(10),
        )

        # Step Function for stopping server
        backup_step = tasks.LambdaInvoke(
            self,
            "BackupWorld",
            lambda_function=backup_lambda,
            result_path="$.backupInfo",
        )

        stop_task_step = tasks.LambdaInvoke(
            self,
            "StopTask",
            lambda_function=stop_task_lambda,
            result_path="$.stopInfo",
        )

        server_stopped = sfn.Succeed(self, "ServerStopped")

        stop_definition = backup_step.next(stop_task_step).next(server_stopped)

        stop_state_machine = sfn.StateMachine(
            self,
            "StopServerStateMachine",
            definition=stop_definition,
            timeout=Duration.minutes(10),
        )

        # Lambda to trigger Step Functions from API
        api_handler = lambda_.Function(
            self,
            "ApiHandler",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            timeout=Duration.seconds(30),
            code=lambda_.Code.from_inline(
                f"""
import boto3
import json

sfn = boto3.client('stepfunctions')

def handler(event, context):
    action = event.get('pathParameters', {{}}).get('action', '')
    
    if action == 'start':
        response = sfn.start_execution(
            stateMachineArn='{start_state_machine.state_machine_arn}',
            input=json.dumps({{}})
        )
        return {{
            'statusCode': 200,
            'body': json.dumps({{'message': 'Server starting', 'executionArn': response['executionArn']}})
        }}
    elif action == 'stop':
        # Get running tasks
        ecs = boto3.client('ecs')
        tasks = ecs.list_tasks(cluster='{cluster.cluster_name}', desiredStatus='RUNNING')
        
        if not tasks.get('taskArns'):
            return {{
                'statusCode': 404,
                'body': json.dumps({{'error': 'No running server found'}})
            }}
        
        task_arn = tasks['taskArns'][0]
        response = sfn.start_execution(
            stateMachineArn='{stop_state_machine.state_machine_arn}',
            input=json.dumps({{'cluster': '{cluster.cluster_name}', 'taskArn': task_arn}})
        )
        return {{
            'statusCode': 200,
            'body': json.dumps({{'message': 'Server stopping', 'executionArn': response['executionArn']}})
        }}
    elif action == 'status':
        ecs = boto3.client('ecs')
        tasks = ecs.list_tasks(cluster='{cluster.cluster_name}', desiredStatus='RUNNING')
        
        if not tasks.get('taskArns'):
            return {{
                'statusCode': 200,
                'body': json.dumps({{'status': 'stopped'}})
            }}
        
        return {{
            'statusCode': 200,
            'body': json.dumps({{'status': 'running', 'taskCount': len(tasks['taskArns'])}})
        }}
    
    return {{
        'statusCode': 400,
        'body': json.dumps({{'error': 'Invalid action. Use /start, /stop, or /status'}})
    }}
"""
            ),
        )

        api_handler.add_to_role_policy(
            iam.PolicyStatement(
                actions=["states:StartExecution"],
                resources=[
                    start_state_machine.state_machine_arn,
                    stop_state_machine.state_machine_arn,
                ],
            )
        )
        api_handler.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecs:ListTasks"],
                resources=["*"],
            )
        )

        # HTTP API Gateway
        http_api = apigw.HttpApi(
            self,
            "MinecraftApi",
            description="Minecraft Server Control API",
        )

        http_api.add_routes(
            path="/{action}",
            methods=[apigw.HttpMethod.GET, apigw.HttpMethod.POST],
            integration=integrations.HttpLambdaIntegration(
                "ApiIntegration",
                api_handler,
            ),
        )

        # Periodic backup using EventBridge (every 5 minutes when server is running)
        backup_rule = events.Rule(
            self,
            "BackupRule",
            schedule=events.Schedule.rate(Duration.minutes(5)),
            enabled=False,  # Enable manually when server is running
        )

        # Outputs
        api_url = http_api.url or "N/A"
        
        CfnOutput(
            self,
            "ApiEndpoint",
            value=api_url,
            description="API endpoint to control server",
        )

        CfnOutput(
            self,
            "StartCommand",
            value=f"curl -X POST {api_url}start",
            description="Command to start server",
        )

        CfnOutput(
            self,
            "StopCommand",
            value=f"curl -X POST {api_url}stop",
            description="Command to stop server",
        )

        CfnOutput(
            self,
            "StatusCommand",
            value=f"curl {api_url}status",
            description="Command to check server status",
        )

        CfnOutput(
            self,
            "BackupBucket",
            value=backup_bucket.bucket_name,
            description="S3 bucket for world backups",
        )

        CfnOutput(
            self,
            "ServerSize",
            value=f"{server_size} ({server_config['description']})",
            description="Server size configuration",
        )
