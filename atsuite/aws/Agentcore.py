import os
import subprocess
import time
import base64
from typing import Optional, Tuple

from dotenv import load_dotenv

load_dotenv()


class AWSAgentCore:
    """
    - 镜像必须为 linux/arm64 平台
    - 容器监听 8000 端口，路径为 /mcp/（由 AgentCore Sidecar 转发）
    - 需要分别创建 Runtime 和 Endpoint
    - 调用 URL 通过 Runtime ARN 构建，需要 SigV4 签名
    """

    def __init__(
        self,
        node_name: str,
        tag: str,
        ecr_client,
        agentcore_client,
        region: str,
        cpu: float = 1.0,
        memory: int = 1024,
        timeout: int = 30,
        image_ref: str | None = None,
        runtime_options: dict | None = None,
        logs_client=None,
    ):
        self.node_name = node_name
        self.tag = tag
        self.ecr_client = ecr_client
        self.agentcore_client = agentcore_client
        self.logs_client = logs_client
        self.region = region
        self.cpu = cpu
        self.memory = memory
        self.timeout = timeout
        self.image_ref = image_ref
        self.runtime_options = dict(runtime_options or {})
        self.strict_experiment = bool(self.runtime_options.get('strict_experiment'))
        self.ownership = self.runtime_options.get('ownership', '')
        if self.runtime_options.get('architecture', 'linux/arm64') != 'linux/arm64':
            raise ValueError('AgentCore container architecture must be linux/arm64')
        if self.strict_experiment and not self.ownership:
            raise ValueError('strict experiment requires ownership')

        self.account_id = os.environ["AWS_ACCOUNT_ID"]
        self.repository_name = os.environ["ECR_REPOSITORY_NAME"]
        self.agentcore_role_arn = os.environ.get(
            "AWS_AGENTCORE_ROLE_ARN",
            f"arn:aws:iam::{self.account_id}:role/atsuite-agentcore-execution-role",
        )
        # AgentCore requires [a-zA-Z][a-zA-Z0-9_]{0,47} (48 chars max). Isolated
        # experiments prefix a 40-char namespace digest, so bound the whole name
        # while keeping the atsuite_at_ prefix that cleanup recognizes.
        node_slug = node_name.lower().replace('-', '_')
        self.runtime_name = f"atsuite_{node_slug}"[:48]
        self._runtime_id: Optional[str] = None

    def deploy(self) -> str:
        """
        推送镜像到 ECR → 创建 Runtime → 创建 Endpoint → 返回调用 URL。
        """
        image_uri = self.image_ref or self._push_to_ecr()
        self._create_or_update_runtime(image_uri)
        self._checkpoint_resource()
        self._ensure_endpoint()
        self._wait_for_runtime_active()
        if self.strict_experiment:
            self._setup_usage_logs()
        url = self._get_invoke_url()
        print(f"\n\n Success deploy on AWS Bedrock AgentCore\n")
        print(f"AgentCore Invoke URL: {url}\n")
        return url


    def _push_to_ecr(self) -> str:
        """推送本地 Docker 镜像到 ECR，返回镜像 URI"""
        docker_image_name = f"{self.tag}:latest"
        ecr_uri = (
            f"{self.account_id}.dkr.ecr.{self.region}.amazonaws.com"
            f"/{self.repository_name}"
        )
        full_image_uri = f"{ecr_uri}:{self.tag}"

        print(f"[ECR] Pushing image to {full_image_uri}")

        # 确保 ECR 仓库存在
        self._ensure_ecr_repository()
        # 登录 ECR
        self._ecr_login()

        subprocess.run(
            ["docker", "tag", docker_image_name, full_image_uri], check=True
        )
        subprocess.run(["docker", "push", full_image_uri], check=True)
        print(f"[ECR] Image pushed successfully")
        return full_image_uri

    def _ensure_ecr_repository(self) -> None:
        """查询 ECR 仓库"""
        try:
            self.ecr_client.describe_repositories(
                repositoryNames=[self.repository_name]
            )
            print(f"[ECR] Repository '{self.repository_name}' exists")
        except self.ecr_client.exceptions.RepositoryNotFoundException:
            print(f"[ECR] Creating repository '{self.repository_name}'...")
            self.ecr_client.create_repository(
                repositoryName=self.repository_name,
                imageScanningConfiguration={"scanOnPush": False},
            )
            print(f"[ECR] Repository created")

    def _ecr_login(self) -> None:
        registry = f"{self.account_id}.dkr.ecr.{self.region}.amazonaws.com"
        try:
            response = self.ecr_client.get_authorization_token()
            auth_data = response["authorizationData"][0]
            token = auth_data["authorizationToken"]
            username, password = (
                base64.b64decode(token).decode("utf-8").split(":")
            )
            subprocess.run(
                [
                    "docker", "login",
                    "--username", username,
                    "--password-stdin",
                    registry,
                ],
                input=password.encode(),
                check=True,
            )
        except Exception as e:
            print(f"[ECR] Warning: Failed to login via API, trying AWS CLI: {e}")
            password_result = subprocess.run(
                ["aws", "ecr", "get-login-password", "--region", self.region],
                capture_output=True,
                text=True,
                check=True,
            )
            subprocess.run(
                [
                    "docker", "login",
                    "--username", "AWS",
                    "--password-stdin",
                    registry,
                ],
                input=password_result.stdout.strip().encode(),
                check=True,
            )


    def _create_or_update_runtime(self, image_uri: str) -> None:
        runtime_params = {
            "agentRuntimeName": self.runtime_name,
            "agentRuntimeArtifact": {
                "containerConfiguration": {
                    "containerUri": image_uri,
                }
            },
            "roleArn": self.agentcore_role_arn,
            "networkConfiguration": {
                "networkMode": "PUBLIC",
            },
            "protocolConfiguration": {
                "serverProtocol": "MCP",
            },
        }
        if self.runtime_options.get('environment'):
            runtime_params['environmentVariables'] = dict(self.runtime_options['environment'])
        if self.ownership:
            runtime_params['tags'] = {'atsuite-ownership': self.ownership}

        try:
            print(f"[AgentCore] Creating Runtime '{self.runtime_name}'...")
            print(f"[AgentCore] Container image: {image_uri}")
            response = self.agentcore_client.create_agent_runtime(**runtime_params)
            self._runtime_id = response["agentRuntimeId"]
            print(
                f"[AgentCore] Runtime created "
                f"(ID: {self._runtime_id}, ARN: {response.get('agentRuntimeArn', 'N/A')})"
            )

        except self.agentcore_client.exceptions.ConflictException:
            print(f"[AgentCore] Runtime '{self.runtime_name}' already exists, updating...")
            runtime_id, _ = self._find_runtime_by_name()
            if not runtime_id:
                raise RuntimeError(
                    f"Runtime '{self.runtime_name}' reported as existing "
                    f"but cannot be found via list_agent_runtimes"
                )
            self._runtime_id = runtime_id
            if self.strict_experiment:
                existing = self.agentcore_client.get_agent_runtime(agentRuntimeId=runtime_id)
                tags = self.agentcore_client.list_tags_for_resource(resourceArn=existing['agentRuntimeArn']).get('tags', {})
                image = existing.get('agentRuntimeArtifact', {}).get('containerConfiguration', {}).get('containerUri')
                if tags.get('atsuite-ownership') != self.ownership or image != image_uri:
                    raise RuntimeError('existing AgentCore runtime has different ownership or image; refusing mutation')
                return
            self.agentcore_client.update_agent_runtime(
                agentRuntimeId=runtime_id,
                agentRuntimeArtifact=runtime_params["agentRuntimeArtifact"],
                roleArn=runtime_params["roleArn"],
                networkConfiguration=runtime_params["networkConfiguration"],
                protocolConfiguration=runtime_params["protocolConfiguration"],
            )
            print(f"[AgentCore] Runtime updated (ID: {runtime_id})")
            # update 后状态会短暂变为 UPDATING，等待重新进入 ACTIVE
            time.sleep(5)

    def _ensure_endpoint(self) -> None:
        try:
            self.agentcore_client.create_agent_runtime_endpoint(
                agentRuntimeId=self._runtime_id,
                name="DEFAULT",
            )
            print(f"[AgentCore] Endpoint 'DEFAULT' created")
        except self.agentcore_client.exceptions.ConflictException:
            print(f"[AgentCore] Endpoint 'DEFAULT' already exists")

    def _find_runtime_by_name(self) -> Tuple[Optional[str], Optional[str]]:
        paginator_args = {}
        while True:
            response = self.agentcore_client.list_agent_runtimes(**paginator_args)
            for rt in response.get("agentRuntimes", []):
                if rt.get("agentRuntimeName") == self.runtime_name:
                    return rt["agentRuntimeId"], rt.get("agentRuntimeArn")
            # 处理分页
            next_token = response.get("nextToken")
            if not next_token:
                break
            paginator_args["nextToken"] = next_token
        return None, None

    def _wait_for_runtime_active(
        self, max_wait: int = 600, interval: int = 15
    ) -> None:
        """ 等待 Runtime READY  """
        print(
            f"[AgentCore] Waiting for Runtime '{self.runtime_name}' "
            f"to become READY (max {max_wait}s)..."
        )
        elapsed = 0
        while elapsed < max_wait:
            response = self.agentcore_client.get_agent_runtime(
                agentRuntimeId=self._runtime_id
            )
            status = response.get("status", "UNKNOWN")
            if status == "READY":
                print(f"[AgentCore] Runtime is READY")
                return
            if status in ("FAILED", "DELETING", "DELETE_FAILED"):
                raise RuntimeError(
                    f"AgentCore Runtime '{self.runtime_name}' "
                    f"entered unexpected status: {status}"
                )
            print(
                f"[AgentCore] Status: {status}, "
                f"waiting {interval}s... ({elapsed}s/{max_wait}s)"
            )
            time.sleep(interval)
            elapsed += interval
        raise TimeoutError(
            f"AgentCore Runtime '{self.runtime_name}' "
            f"did not become ACTIVE within {max_wait}s"
        )

    def _get_invoke_url(self) -> str:
        response = self.agentcore_client.get_agent_runtime(
            agentRuntimeId=self._runtime_id
        )
        runtime_arn = response["agentRuntimeArn"]
        encoded_arn = runtime_arn.replace(":", "%3A").replace("/", "%2F")
        return (
            f"https://bedrock-agentcore.{self.region}.amazonaws.com"
            f"/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
        )

    # AgentCore bills the runtime from USAGE_LOGS, which are only delivered to
    # CloudWatch once a delivery source/destination/delivery exists. ATSuite's
    # analyzer reads that log group for session cost, so an experiment runtime
    # without it produces no usage evidence and the ingestion wait just times
    # out. Set it up before the first invocation.
    USAGE_LOG_GROUP_PREFIX = "/aws/vendedlogs/bedrock-agentcore/runtime/USAGE_LOGS"

    @staticmethod
    def _usage_delivery_suffix(runtime_id: str) -> str:
        import hashlib

        return hashlib.sha256(runtime_id.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def usage_log_group(cls, runtime_id: str) -> str:
        return f"{cls.USAGE_LOG_GROUP_PREFIX}/{runtime_id}"

    @classmethod
    def usage_delivery_names(cls, runtime_id: str) -> Tuple[str, str]:
        suffix = cls._usage_delivery_suffix(runtime_id)
        return f"atsuite-usage-source-{suffix}", f"atsuite-usage-destination-{suffix}"

    @staticmethod
    def _ignore_delivery_conflict(action):
        try:
            return action()
        except Exception as exc:  # noqa: BLE001 - classify by AWS error code
            markers = {"ConflictException", "ResourceAlreadyExistsException", "AlreadyExists"}
            code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
            if code in markers or type(exc).__name__ in markers or "already exists" in str(exc).lower():
                return
            raise

    def _checkpoint_resource(self, **details) -> None:
        callback = self.runtime_options.get('on_resource')
        if callback:
            callback({'physical_name':self.runtime_name,'runtime_id':self._runtime_id,**details})

    @staticmethod
    def _read_usage_resource(action):
        try:
            return action()
        except Exception as exc:
            code = getattr(exc, 'response', {}).get('Error', {}).get('Code', '')
            if code == 'ResourceNotFoundException' or type(exc).__name__ == 'ResourceNotFoundException':
                return None
            raise

    def _setup_usage_logs(self) -> None:
        import boto3

        runtime_id = self._runtime_id
        if not runtime_id:
            raise RuntimeError("AgentCore usage logs require a created runtime")
        logs = self.logs_client or boto3.client("logs", region_name=self.region)
        runtime_arn = self.agentcore_client.get_agent_runtime(agentRuntimeId=runtime_id)["agentRuntimeArn"]
        log_group = self.usage_log_group(runtime_id)
        log_group_arn = f"arn:aws:logs:{self.region}:{self.account_id}:log-group:{log_group}"
        source_name, destination_name = self.usage_delivery_names(runtime_id)
        destination_arn = f"arn:aws:logs:{self.region}:{self.account_id}:delivery-destination:{destination_name}"
        resources = {'log_group':log_group,'source_name':source_name,'destination_name':destination_name,
                     'runtime_arn':runtime_arn,'destination_arn':destination_arn}
        # Checkpoint intent before creating child resources, including failed deploys.
        self._checkpoint_resource(usage_logs=resources)
        tags = {'atsuite-ownership':self.ownership}
        group = self._read_usage_resource(lambda: logs.list_tags_for_resource(resourceArn=log_group_arn))
        source = self._read_usage_resource(lambda: logs.get_delivery_source(name=source_name))
        destination = self._read_usage_resource(lambda: logs.get_delivery_destination(name=destination_name))
        for existing in (group, source and source['deliverySource'], destination and destination['deliveryDestination']):
            if existing is not None and existing.get('tags', {}).get('atsuite-ownership') != self.ownership:
                raise RuntimeError('AgentCore usage logs have different ownership; refusing mutation')
        if source and (source['deliverySource'].get('logType') != 'USAGE_LOGS' or
                       source['deliverySource'].get('resourceArns') != [runtime_arn]):
            raise RuntimeError('AgentCore usage source configuration mismatch')
        if destination and destination['deliveryDestination'].get('deliveryDestinationConfiguration', {}).get('destinationResourceArn') != log_group_arn:
            raise RuntimeError('AgentCore usage destination configuration mismatch')
        if group is None:
            self._ignore_delivery_conflict(lambda: logs.create_log_group(logGroupName=log_group, tags=tags))
        if source is None:
            self._ignore_delivery_conflict(lambda: logs.put_delivery_source(name=source_name, resourceArn=runtime_arn, logType="USAGE_LOGS", tags=tags))
        if destination is None:
            self._ignore_delivery_conflict(lambda: logs.put_delivery_destination(
                name=destination_name,
                deliveryDestinationConfiguration={"destinationResourceArn": log_group_arn},
                outputFormat="json", tags=tags,
            ))
        delivery = self._ignore_delivery_conflict(lambda: logs.create_delivery(
            deliverySourceName=source_name,
            deliveryDestinationArn=destination_arn, tags=tags,
        ))
        if delivery:
            self._checkpoint_resource(usage_logs={**resources,'delivery_id':delivery['delivery']['id']})
        print(f"[AgentCore] USAGE_LOGS delivery configured -> {log_group}")
