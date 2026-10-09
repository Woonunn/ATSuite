import json
import os
import requests

from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from atsuite.pipeline import RuntimeConfig
from atsuite.utils import normalize_node_name
from atsuite.faas.config import function_config_path, load_function_config


def _cloud_run_authorization(url: str) -> dict:
    parsed = urlparse(url)
    if not (parsed.hostname or '').endswith('.run.app'):
        return {}
    from google.auth.transport.requests import Request
    from google.oauth2.id_token import fetch_id_token
    token = fetch_id_token(Request(), f'{parsed.scheme}://{parsed.netloc}')
    return {'Authorization': 'Bearer ' + token}


def _runtime_options(runtime, supplied=None):
    options = {key: value for key, value in (supplied or {}).items() if value is not None and value != ''}
    for name in ('architecture', 'concurrency', 'billing_mode', 'execution_environment', 'min_instances',
                 'session_affinity', 'session_idle_timeout_s', 'session_ttl_s'):
        value = getattr(runtime, name, None)
        if value not in ('', None):
            options[name] = value
    return options


def _cloud_run_readback(fn):
    import subprocess
    service_name = fn.function_name.replace('_', '-').lower()
    response = subprocess.run(['gcloud', 'run', 'services', 'describe', service_name, '--region', fn.region,
        '--project', fn.project_id, '--format', 'json'], capture_output=True, text=True, check=True)
    payload = json.loads(response.stdout)
    metadata = payload.get('metadata', {})
    template = payload.get('spec', {}).get('template', {})
    annotations = template.get('metadata', {}).get('annotations', {})
    pod = template.get('spec', {})
    containers = pod.get('containers') or []
    container = containers[0] if containers else {}
    limits = container.get('resources', {}).get('limits', {})
    effective = {}
    if limits.get('cpu') is not None:
        value = str(limits['cpu'])
        effective['cpu'] = float(value[:-1]) / 1000 if value.endswith('m') else float(value)
    if limits.get('memory') is not None:
        value = str(limits['memory'])
        effective['memory'] = int(float(value[:-2]) * 1024) if value.endswith('Gi') else int(float(value[:-2])) if value.endswith('Mi') else int(value)
    for field, key in (('timeout', 'timeoutSeconds'), ('concurrency', 'containerConcurrency')):
        if pod.get(key) is not None:
            effective[field] = int(pod[key])
    if annotations.get('run.googleapis.com/execution-environment'):
        effective['execution_environment'] = annotations['run.googleapis.com/execution-environment']
    if annotations.get('run.googleapis.com/cpu-throttling') is not None:
        effective['billing_mode'] = 'request_based' if str(annotations['run.googleapis.com/cpu-throttling']).lower() == 'true' else 'instance_based'
    image = payload.get('status', {}).get('imageDigest') or container.get('image', '')
    ready = next((condition.get('status') for condition in payload.get('status', {}).get('conditions', []) if condition.get('type') == 'Ready'), None)
    return {'effective_resources': effective, 'configuration_readback': 'partial',
        'physical_resource_id': str(metadata.get('uid') or service_name), 'physical_name': service_name,
        'image_digest': image.split('@')[-1] if '@' in image else image if image.startswith('sha256:') else None,
        'effective_region': metadata.get('labels', {}).get('cloud.googleapis.com/location'),
        'runtime_status': 'READY' if ready == 'True' else 'NOT_READY' if ready is not None else 'UNKNOWN',
        'ownership': metadata.get('labels', {}).get('atsuite-ownership'),
        'field_semantics': {'disk': 'provider_managed', 'architecture': 'platform_fixed_amd64'},
        'observed_environment': {entry['name']: entry.get('value') for entry in container.get('env', [])}}


def _ali_readback(fn):
    from alibabacloud_fc20230330 import models
    from alibabacloud_tea_util.models import RuntimeOptions
    response = fn.client.get_function_with_options(fn.function_name, models.GetFunctionRequest(), {}, RuntimeOptions())
    payload = response.body.to_map()
    effective = {}
    for source, target in (('cpu', 'cpu'), ('memorySize', 'memory'), ('diskSize', 'disk'), ('timeout', 'timeout'), ('instanceConcurrency', 'concurrency')):
        if payload.get(source) is not None:
            effective[target] = payload[source]
    image = payload.get('customContainerConfig', {}).get('image', '')
    arn = payload.get('functionArn', '')
    return {'effective_resources': effective, 'configuration_readback': 'partial',
        'physical_resource_id': arn or payload.get('functionId') or fn.function_name, 'physical_name': fn.function_name,
        'image_digest': image.split('@')[-1] if '@' in image else None,
        'effective_region': arn.split(':')[3] if arn.count(':') >= 3 else None,
        'runtime_status': payload.get('state') or payload.get('lastUpdateStatus') or 'UNKNOWN',
        'ownership': next((tag.get('value', tag.get('Value')) for tag in payload.get('tags', []) if tag.get('key', tag.get('Key')) == 'atsuite-ownership'), None),
        'field_semantics': {'architecture': 'platform_fixed_amd64'}, 'observed_environment': payload.get('environmentVariables', {})}

try:
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.session import Session as BotocoreSession
except ImportError:  # pragma: no cover - exercised only when aws extras are absent
    SigV4Auth = None
    AWSRequest = None
    BotocoreSession = None


class FunctionClient:
    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self._aws_credentials = None
        self.timeout = 1800

        # Use a plain session without automatic retries.
        self.session = requests.Session()

    def invoke(self, tool_name: str, args: dict, request_id: Optional[str] = None):
        tool_name_safe = tool_name.replace(".", "_")
        payload = {
            "tool": tool_name_safe,
            "args": args,
        }

        invoke_url = f"{self.url}/run"
        if self._is_aws_lambda_function_url():
            body = json.dumps(payload)
            headers = self._build_aws_lambda_sigv4_headers(
                invoke_url,
                body,
                request_id=request_id,
            )
            response = self.session.post(
                invoke_url,
                data=body,
                headers=headers,
                timeout=self.timeout,
            )
        else:
            headers = _cloud_run_authorization(self.url)
            if request_id:
                headers["X-Request-Id"] = request_id
            response = self.session.post(
                invoke_url,
                json=payload,
                headers=headers or None,
                timeout=self.timeout,
            )

        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as err:
            raise RuntimeError(
                self._format_response_error(
                    response,
                    invoke_url,
                    prefix=f"Function invoke failed with HTTP {response.status_code}",
                )
            ) from err

        try:
            rsp = response.json()
        except requests.exceptions.JSONDecodeError as err:
            raise RuntimeError(
                self._format_response_error(
                    response,
                    invoke_url,
                    prefix="Function invoke returned non-JSON response",
                )
            ) from err

        result = rsp.get("result")
        if isinstance(result, str):
            stripped = result.strip()
            if stripped and stripped[0] in "[{":
                try:
                    result = json.loads(stripped)
                except json.JSONDecodeError:
                    pass
        if isinstance(result, dict) and result.get("isError"):
            raise RuntimeError(f"Tool {tool_name} returned isError: {result}")

        # Ali Cloud: X-Fc-Request-Id
        # AWS Lambda: x-amzn-requestid
        request_id = (
            response.headers.get("X-Fc-Request-Id")
            or response.headers.get("x-amzn-requestid")
            or request_id
        )

        is_stateful = response.headers.get("X-Tool-Stateful", "false").lower() == "true"

        return request_id, is_stateful, result

    @staticmethod
    def _format_response_error(
        response: requests.Response,
        invoke_url: str,
        *,
        prefix: str,
    ) -> str:
        content_type = response.headers.get("Content-Type", "").strip() or "unknown"
        preview = " ".join(response.text.strip().split())
        if len(preview) > 240:
            preview = preview[:237] + "..."
        return (
            f"{prefix} from {invoke_url} "
            f"(content-type: {content_type}): {preview or '<empty body>'}"
        )

    # class FunctionClient:
    #     def __init__(self, url: str):
    #         self.url = url.rstrip("/")

    #     def invoke(self, tool_name: str, args: dict, request_id: Optional[str] = None):
    #         tool_name_safe = tool_name.replace(".", "_")
    #         headers = {}
    #         if request_id:
    #             headers["X-Request-Id"] = request_id
    #         response = requests.post(
    #             f"{self.url}/run",
    #             json={
    #                 "tool": tool_name_safe,
    #                 "args": args,
    #             },
    #             headers=headers or None,
    #         )
    #         response.raise_for_status()
    #         # Ali Cloud: X-Fc-Request-Id, AWS Lambda: x-amzn-requestid
    #         request_id = response.headers.get("X-Fc-Request-Id") or response.headers.get("x-amzn-requestid") or request_id
    #         is_stateful = response.headers.get("X-Tool-Stateful", "false").lower() == "true"
    #         return request_id, is_stateful
    #         # return response.json()

    def _is_aws_lambda_function_url(self) -> bool:
        host = urlparse(self.url).netloc.lower()
        return ".lambda-url." in host and host.endswith(".on.aws")

    def _lambda_region(self) -> str:
        host = urlparse(self.url).netloc.lower()
        marker = ".lambda-url."
        suffix = ".on.aws"
        if marker in host and host.endswith(suffix):
            start = host.index(marker) + len(marker)
            end = host.rfind(suffix)
            region = host[start:end]
            if region:
                return region
        return os.environ.get("AWS_REGION", "us-east-1")

    def _get_aws_credentials(self):
        if self._aws_credentials is None:
            if BotocoreSession is None:
                raise RuntimeError(
                    "botocore is required for AWS Lambda Function URL IAM auth"
                )
            credentials = BotocoreSession().get_credentials()
            if credentials is None:
                raise RuntimeError(
                    "AWS credentials are required for Lambda Function URL IAM auth"
                )
            self._aws_credentials = credentials.get_frozen_credentials()
        return self._aws_credentials

    def _build_aws_lambda_sigv4_headers(
        self,
        invoke_url: str,
        body: str,
        *,
        request_id: Optional[str] = None,
    ) -> dict:
        if SigV4Auth is None or AWSRequest is None:
            raise RuntimeError(
                "botocore is required for AWS Lambda Function URL IAM auth"
            )

        headers = {"Content-Type": "application/json"}
        if request_id:
            headers["X-Request-Id"] = request_id

        aws_request = AWSRequest(
            method="POST",
            url=invoke_url,
            headers=headers,
            data=body,
        )
        SigV4Auth(
            self._get_aws_credentials(),
            "lambda",
            self._lambda_region(),
        ).add_auth(aws_request)
        return dict(aws_request.headers.items())


class AliFunctionDeployer:
    def __init__(self, bench_name: str, *, region: str = ""):
        from atsuite.ali.ali import Ali

        self.bench_name = bench_name
        self.ali = Ali(region=region or None)
        self._targets = {}

    def deploy_target(
        self, target_name: str, runtime_config: RuntimeConfig, image_ref: str | None = None, *, runtime_options: dict | None = None
    ) -> Optional[str]:
        node_name = normalize_node_name(target_name)
        tag = f"atsuite-function-{self.bench_name.lower()}-{node_name.lower()}"
        fn = self.ali.deploy_function(
            function_name=node_name,
            entrypoint=["python", "-m", "atsuite_sdk.function"],
            tag=tag,
            image_ref=image_ref,
            runtime_options=_runtime_options(runtime_config, runtime_options),
            runtime="custom-container",
            cpu=runtime_config.cpu,
            memory_size=runtime_config.memory,
            disk_size=runtime_config.disk,
            timeout=runtime_config.timeout,
        )
        endpoint = fn.deploy()
        self._targets[node_name] = fn
        return endpoint

    def readback_target(self, target_name: str) -> dict:
        return _ali_readback(self._targets[normalize_node_name(target_name)])

    def deploy_node(self, node_name: str, node_dir: Path) -> Optional[str]:
        config_path = function_config_path(node_dir)
        fun_config = load_function_config(config_path)
        return self.deploy_target(node_name, fun_config)


class GCPFunctionDeployer:
    def __init__(self, bench_name: str, *, region: str = ""):
        from atsuite.gcp.gcp import GCP

        self.bench_name = bench_name
        self.gcp = GCP(region=region or None)
        self._targets = {}

    def deploy_target(
        self, target_name: str, runtime_config: RuntimeConfig, image_ref: str | None = None, *, runtime_options: dict | None = None
    ) -> Optional[str]:
        node_name = normalize_node_name(target_name)
        tag = f"atsuite-function-{self.bench_name.lower()}-{node_name.lower()}"
        fn = self.gcp.deploy_function(
            function_name=node_name,
            entrypoint=["python", "-m", "atsuite_sdk.function"],
            tag=tag,
            image_ref=image_ref,
            runtime_options=_runtime_options(runtime_config, runtime_options),
            runtime="custom-container",
            cpu=runtime_config.cpu,
            memory_size=runtime_config.memory,
            disk_size=runtime_config.disk,
            timeout=runtime_config.timeout,
        )
        endpoint = fn.deploy()
        self._targets[node_name] = fn
        return endpoint

    def readback_target(self, target_name: str) -> dict:
        return _cloud_run_readback(self._targets[normalize_node_name(target_name)])

    def deploy_node(self, node_name: str, node_dir: Path) -> Optional[str]:
        config_path = function_config_path(node_dir)
        fun_config = load_function_config(config_path)
        return self.deploy_target(node_name, fun_config)


class AWSFunctionDeployer:
    def __init__(self, bench_name: str, *, region: str = ""):
        self.bench_name = bench_name
        from atsuite.aws.aws import AWS

        self.aws = AWS(region=region or None)
        self._targets = {}

    def deploy_target(
        self, target_name: str, runtime_config: RuntimeConfig, image_ref: str | None = None, *, runtime_options: dict | None = None
    ) -> Optional[str]:
        node_name = normalize_node_name(target_name)
        tag = f"atsuite-function-{self.bench_name.lower()}-{node_name.lower()}"

        fn = self.aws.deploy_lambda(
            function_name=node_name,
            entrypoint=["python", "-m", "atsuite_sdk.function"],
            tag=tag,
            image_ref=image_ref,
            runtime_options=_runtime_options(runtime_config, runtime_options),
            runtime="custom-container",
            cpu=runtime_config.cpu,
            memory_size=runtime_config.memory,
            timeout=runtime_config.timeout,
            disk_size=runtime_config.disk,
        )
        endpoint = fn.deploy()
        self._targets[node_name] = fn
        return endpoint

    def readback_target(self, target_name: str) -> dict:
        fn = self._targets[normalize_node_name(target_name)]
        response = fn.lambda_client.get_function(FunctionName=fn.function_name)
        config = response.get('Configuration', {})
        effective = {}
        if config.get('MemorySize') is not None:
            effective['memory'] = int(config['MemorySize'])
            effective['cpu'] = round(effective['memory'] / 1769, 3)
        if config.get('Timeout') is not None:
            effective['timeout'] = int(config['Timeout'])
        storage = config.get('EphemeralStorage', {}).get('Size')
        if storage is not None:
            effective['disk'] = int(storage)
        architectures = config.get('Architectures') or []
        if architectures:
            effective['architecture'] = 'linux/arm64' if architectures[0] == 'arm64' else 'linux/amd64'
        arn = str(config.get('FunctionArn', ''))
        image = response.get('Code', {}).get('ResolvedImageUri') or response.get('Code', {}).get('ImageUri', '')
        tags = fn.lambda_client.list_tags(Resource=arn).get('Tags', {}) if arn else {}
        return {'effective_resources': effective, 'configuration_readback': 'partial',
            'physical_resource_id': arn or fn.function_name, 'physical_name': fn.function_name,
            'image_digest': image.split('@')[-1] if '@' in image else None,
            'effective_region': arn.split(':')[3] if arn.count(':') >= 3 else None,
            'runtime_status': config.get('State') or 'UNKNOWN', 'ownership': tags.get('atsuite-ownership'),
            'field_semantics': {'cpu': 'official_rule_derived', 'memory': 'settable', 'disk': 'settable'},
            'observed_environment': config.get('Environment', {}).get('Variables', {})}

    def deploy_node(self, node_name: str, node_dir: Path) -> Optional[str]:
        config_path = function_config_path(node_dir)
        runtime_config = load_function_config(config_path)
        return self.deploy_target(node_name, runtime_config)
