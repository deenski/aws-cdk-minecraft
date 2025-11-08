# AWS CDK Minecraft Server (Python)

This repository contains an AWS CDK (Python) project that deploys a cost-optimized
Minecraft server on AWS using ECS Fargate, S3 backups, and related automation. It is far outside of the scope of this document to teach you how to use AWS. As such I assume no responsibility for the consequences your actions. 

Review `app.py` and `minecraft_server/minecraft_server_stack.py` for configuration and deployment details.

## Quick start

1. Configure any context values in `cdk.context.json` or use the example file
	`cdk.context.json.example`.
2. (Optional) Create and activate a virtualenv, then install deps.
	```
	python3 -m venv .venv
	source .venv/bin/activate
	pip install -r requirements.txt
	```
3. Synthesize and deploy:
	```
	cdk synth
	cdk deploy
	```
4. Go play!
5. `curl $MINECRAFT_HOST/stop`
6. To play again: `curl $MINECRAFT_HOST/start`

## More info
The full documentation for this repo can be found in [docs/MC_SERVER_STACK.md](docs/MC_SERVER_STACK.md)

### Credits

This project uses the Docker image maintained by itzg: `itzg/minecraft-server`.
For the full set of configuration options and runtime behavior for that image,
see the upstream documentation:

https://docker-minecraft-server.readthedocs.io/en/latest/
