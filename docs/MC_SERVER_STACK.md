
# Minecraft Server Stack — Detailed Guide

This document explains the architecture, configuration, operational details, backup options, billing implications, and troubleshooting tips for the `MinecraftServerStack` implemented in `minecraft_server/minecraft_server_stack.py`.

Use this guide when deploying, operating, or modifying the stack. For configuration flags and image runtime options, also consult the upstream itzg image docs:

https://docker-minecraft-server.readthedocs.io/en/latest/

## High level architecture

The stack is intentionally cost-optimized and minimalist. Key components:

- VPC: small VPC with public subnets only (no NAT gateways) to avoid NAT gateway charges.
- ECS Cluster: an ECS cluster running Fargate tasks for the Minecraft server.
- Fargate Task / Container: the `itzg/minecraft-server` container runs the game server.
- S3: bucket for periodic world backups.
- Lambda: small functions to start/stop the ECS service, get the public IP, and trigger backups.
- Step Functions: orchestrates task start, wait, IP retrieval and optional DNS updates.
- API Gateway (v2): provides HTTP endpoints to control the server remotely.
- Route53 (optional): updates DNS A record with the current server IP when enabled.
- Budgets: an AWS Budgets alarm to notify when monthly costs exceed a configured threshold.

ASCII diagram

```
	[User/API] --> [API Gateway] --> [Start/Stop Lambdas] --> [ECS Fargate Service]
																						 |                         |
																						 |                         ---> [CloudWatch Logs]
																						 ---> [StepFunctions] --> [GetIP Lambda]
																													 |
																													 ---> [UpdateDNS Lambda (optional)]

	[ECS Container] --writes--> [S3 Backup Bucket]
```

## Configuration (cdk.context.json)

The stack reads configuration from the CDK context key `minecraft` (see `cdk.context.json.example`). Important keys:

- `server_size`: `small|medium|large` — selects CPU/memory task sizing and cost profile.
- `enable_route53`: `true|false` — whether to enable DNS updates via Route53.
- `hosted_zone_id` / `domain_name`: values used when `enable_route53` is true.
- `budget_email`: email address used for budget alerts.
- `budget_amount`: monthly budget threshold (USD) to trigger an alert.
- `allowed_cidrs`: list of CIDR blocks allowed to reach the Minecraft port (default `0.0.0.0/0`).
- `variables`: dict of environment variables that get injected into the container. Typical values include:
	- `MOTD` — server message-of-the-day
	- `TYPE` — server type (VANILLA, PAPER, FORGE, AUTO_CURSEFORGE, etc.)
	- `VERSION` — game/server version or `LATEST`
	- image-specific options like `CF_API_KEY`, `CF_SLUG` (if using CurseForge automation)

Notes:
- Do NOT put long-term secrets (API keys, passwords) in plaintext in `cdk.context.json`. Use AWS Secrets Manager, SSM Parameter Store or CDK Secrets mechanisms for production.

## Enabling and operating S3 backups

This stack creates an S3 bucket and injects its name into the container via the `S3_BUCKET` environment variable. A `BackupFunction` Lambda is included to coordinate or record backups.

How backups are intended to work (recommended flow):

1. Tell the Minecraft server to persist the world to disk (in-game command `save-all` or equivalent). The container image supports built-in helper scripts; consult the upstream docs for the exact helper names.
2. Compress or archive the world directory inside the running container.
3. Upload the archive to S3 using the AWS CLI or SDK with the bucket name provided in `S3_BUCKET`.
4. Remove temporary archives from the task filesystem.

The stack provides a few ways to trigger a backup:

- Manually via ECS Execute Command (recommended for ad-hoc backups).
- Invoke the `BackupFunction` Lambda (the function can be extended to call `ecs:ExecuteCommand` or coordinate a safe backup sequence).
- Add an EventBridge (CloudWatch Events) rule to schedule regular backups (e.g., daily at 04:00 UTC) that invoke the backup Lambda.

Example: run a backup using ECS execute-command (high-level example):

```bash
# Find the running task ARN for the service
aws ecs list-tasks --cluster MinecraftECSCluster --service-name MinecraftService --desired-status RUNNING

# Execute a command in the container (requires AWS CLI v2 and SSM Session Manager plugin)
aws ecs execute-command \
	--cluster MinecraftECSCluster \
	--task <task-arn> \
	--container MinecraftContainer \
	--interactive \
	--command "bash -lc 'save-all && tar -czf /tmp/world-$(date -u +%Y%m%dT%H%M%SZ).tar.gz world && aws s3 cp /tmp/world-$(date -u +%Y%m%dT%H%M%SZ).tar.gz s3://<bucket>/backups/ && rm /tmp/world-*.tar.gz'"
```

Important backup considerations:

- Consistency: you should run `save-off`/`save-on` around a backup or use the server's built-in save command to ensure world state consistency.
- If multiple players are online, consider warning or putting the server in a read-only state during backup to avoid corrupting the backup.
- Storage costs: S3 charges for storage and PUT/GET requests. Keep an eye on backup size and retention.
- Lifecycle rules: the stack includes a lifecycle rule to expire backups after a short period; adjust this to your retention needs and compliance requirements.

## Billing implications and cost control

Primary cost drivers:

- ECS Fargate (compute + memory): charged per vCPU and GB-hour. This stack uses Fargate (spot where possible) to reduce cost. Spot can save ~50–70% but is not guaranteed and tasks can be interrupted.
- Data transfer: public IPs and internet egress are charged. If you expect many players and lots of outbound traffic, this can dominate costs.
- S3 storage and requests: backups and any world artifacts are billed per GB-month and per request.
- Lambda invocations: small but can add up if running frequently.
- Step Functions: billed per state transition which can be non-negligible if workflow runs are frequent.
- API Gateway: request pricing for the control API.

Cost control recommendations:

- Use small task sizes for low-player counts and scale up only when needed.
- Prefer Fargate Spot for non-critical, on-demand servers. For critical uptime, use standard Fargate.
- Limit access to the Minecraft port (`25565`) using `allowed_cidrs` so you don't expose the server to Web-scale scanning.
- Configure S3 lifecycle rules to limit retention and move older backups to cheaper storage classes (Glacier/IA) if needed.
- Use the built-in budget alarm (`budget_amount` / `budget_email`) to receive warnings when spending approaches your monthly limit.
- Monitor CloudWatch metrics and create cost alarms for unexpected spikes.

Example cost-estimate checklist (ballpark):

- Fargate (small): CPU+memory hours * Fargate rate (check AWS Pricing for region) 
- S3: backup size * retention months * S3 price per GB-month
- Lambda and Step Functions: count * per-request / per-transition price

Use the AWS Pricing Calculator for accurate estimates based on your region and usage patterns.

## Security and secrets

- Network: The stack uses a security group that opens the Minecraft port to `allowed_cidrs`. Restrict this to the IPs you expect to use.
- Secrets: Do not store secrets (API keys, tokens) in plaintext in `cdk.context.json`. Use AWS Secrets Manager or SSM Parameter Store and grant the Task/TaskRole the necessary permissions.
- IAM: The task and Lambdas use roles with scoped permissions. Review these if you extend functionality. Minimize `"*"` permissions where possible.
- S3: the backup bucket blocks public access and uses S3-managed encryption. Consider using a customer-managed KMS key if you have compliance requirements.

## Logs and monitoring

- Container logs are shipped to CloudWatch Logs with a 1-week retention by default; adjust retention as needed.
- CloudWatch Container Insights are turned off by default to save cost. If you want deeper metrics, enable Container Insights on the cluster.
- Use CloudWatch Alarms for unhealthy services, task failures, or budget threshold breaches.

## Route53 and DNS

If `enable_route53` is true and valid `hosted_zone_id`/`domain_name` are provided, the stack can automatically UPSERT an A record pointing to the public IP of the running task. Considerations:

- DNS propagation: changes are near-instant for the A record, but caching may cause delays for clients.
- IP changes: because tasks are ephemeral and may get new public IPs when restarted, DNS updates are necessary every time the server starts — the included `GetIP` and `UpdateDNS` Lambdas handle this.

## Operational playbook

Start the server (high level):

1. `cdk deploy` the stack (or `cdk synth` then deploy via console).
2. Use the provided API endpoint or `StartTaskFunction` Lambda to set the ECS Service desired count to 1.
3. Wait for the task to be healthy. The Step Function or `GetIP` Lambda can retrieve the public IP.
4. Connect with your Minecraft client to `domain_name` (if using Route53) or the public IP + port.

Stop the server safely:

1. Use the API or `StopTaskFunction` Lambda to set desired count to 0. This gracefully removes the task.
2. Ensure a final backup runs before termination if you want the latest world state preserved.

Maintenance tasks:

- Upgrade the container image by changing the image tag in `minecraft_server_stack.py` or passing environment variable overrides via `variables`.
- Tune CPU/memory in `cdk.context.json` by selecting appropriate `server_size`.
- Add scheduled backups using EventBridge rules.

## Troubleshooting

Problem: Task never reaches RUNNING or keeps restarting
- Check CloudWatch Logs for the container to see startup errors (out of memory, missing files, invalid args).
- Verify the task IAM role has the permissions required by the container (S3 access, if needed).
- Check that the container's MEMORY setting is sufficient for the chosen server flavor and mods.

Problem: Backups are missing or empty
- Ensure the backup commands were executed after `save-all` and that the tar/zip included the correct world paths.
- Confirm the TaskRole or Lambda role has `s3:PutObject` permission for the backup prefix.

Problem: Cannot execute execute-command
- Ensure you have AWS CLI v2 and the Session Manager plugin installed and configured.
- Ensure the task has `enableExecuteCommand` and the task role permissions for `ssm:StartSession` and related APIs.

Problem: Route53 record not updated
- Confirm `enable_route53` is set to `true` and `hosted_zone_id`/`domain_name` are correct and the UpdateDNS Lambda has `route53:ChangeResourceRecordSets` permission.

Problem: Unexpected costs
- Check CloudWatch metrics and AWS Cost Explorer. Look for prolonged Fargate runtime, large data transfer, or many backups.

## Example context snippet

Add or edit `cdk.context.json` `minecraft` block:

```json
"minecraft": {
	"server_size": "small",
	"enable_route53": false,
	"hosted_zone_id": "",
	"domain_name": "",
	"budget_email": "your-email@example.com",
	"budget_amount": 10,
	"allowed_cidrs": ["203.0.113.0/24"],
	"variables": {
		"MOTD": "Welcome to my server!",
		"TYPE": "PAPER",
		"VERSION": "1.20.1"
	}
}
```

## Where to find more

- Upstream Docker image docs (runtime options, helper scripts):
	https://docker-minecraft-server.readthedocs.io/en/latest/
- AWS CDK docs: https://docs.aws.amazon.com/cdk/latest/guide/home.html
