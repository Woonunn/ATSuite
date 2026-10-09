# GCP Cloud Run 部署：镜像推送到 Artifact Registry / GCR，gcloud run deploy 返回 URL

import os
import json
import subprocess
from typing import Any, Optional

from atsuite.function import FunctionBase
from atsuite.utils import run


_CLOUD_RUN_MAX_TIMEOUT_SECONDS = 3600


def _get_project() -> str:
    p = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not p:
        raise RuntimeError("not set GOOGLE_CLOUD_PROJECT")
    return p


def _get_region() -> str:
    return os.environ.get("GCP_REGION", "us-central1")


def _cloud_run_service_name(name: str) -> str:
    # normalize service name, gcloud run service name only allows lowercase letters, numbers and -
    return name.replace("_", "-").lower()


def _image_uri(project_id: str, tag: str) -> str:
    """镜像完整 URI。优先 GCP_IMAGE_PREFIX（如 us-central1-docker.pkg.dev/PROJECT/repo），否则 gcr.io/PROJECT。"""
    # image uri, prefer GCP_IMAGE_PREFIX (like us-central1-docker.pkg.dev/PROJECT/repo)
    prefix = os.environ.get("GCP_IMAGE_PREFIX")
    if prefix:
        return f"{prefix.rstrip('/')}/{tag}:latest"
    return f"gcr.io/{project_id}/{tag}:latest"


class GCPFC(FunctionBase):
    """Cloud Run 部署：本地镜像 tag → push → gcloud run deploy，返回服务 URL。"""
    # deploy function or mcp on GCP Cloud Run
    def __init__(
        self,
        function_name: str,
        entrypoint: list,
        tag: str,
        project_id: str,
        region: str,
        typ: str = "function",
        cpu: float = 1.0,
        memory_size: int = 1024,
        timeout: int = 60,
        session_affinity: Optional[bool] = None,
        min_instances: Optional[int] = None,
        image_ref: Optional[str] = None,
        runtime_options: dict | None = None,
        **kwargs: Any,
    ):
        self.function_name = function_name
        self.entrypoint = entrypoint
        self.tag = tag
        self.project_id = project_id
        self.region = region
        self.typ = typ
        self.cpu = cpu
        self.memory_size = memory_size
        self.timeout = timeout
        self.session_affinity = session_affinity
        self.min_instances = min_instances
        self.image_ref = image_ref
        self.runtime_options = dict(runtime_options or {})
        self.min_instances = self.runtime_options.get('min_instances', self.min_instances)
        self.session_affinity = self.runtime_options.get('session_affinity', self.session_affinity)
        self.strict_experiment = bool(self.runtime_options.get('strict_experiment'))
        self.ownership = str(self.runtime_options.get('ownership', ''))[:63]
        if not 1 <= timeout <= _CLOUD_RUN_MAX_TIMEOUT_SECONDS:
            raise ValueError('Cloud Run timeout outside supported range')
        if self.strict_experiment and not self.ownership:
            raise ValueError('strict experiment requires ownership')

    def deploy(self) -> str:
        # tag local image to image uri and push to registry
        image_uri = self.image_ref or _image_uri(self.project_id, self.tag)
        if not self.image_ref:
            local_ref = f"{self.tag}:latest"
            run(["docker", "tag", local_ref, image_uri])
            run(["docker", "push", image_uri])

        # deploy function or mcp on GCP Cloud Run
        service_name = _cloud_run_service_name(self.function_name)
        if self.strict_experiment:
            existing = subprocess.run(['gcloud', 'run', 'services', 'describe', service_name, '--region', self.region,
                '--project', self.project_id, '--format', 'json'], capture_output=True, text=True)
            if existing.returncode == 0:
                payload = json.loads(existing.stdout)
                labels = payload.get('metadata', {}).get('labels', {})
                containers = payload.get('spec', {}).get('template', {}).get('spec', {}).get('containers', [])
                if labels.get('atsuite-ownership') != self.ownership or not containers or containers[0].get('image') != image_uri:
                    raise RuntimeError('existing Cloud Run service has different ownership or image; refusing mutation')
                return str(payload.get('status', {}).get('url', '')).rstrip('/')
            if 'not found' not in existing.stderr.lower() and 'not_found' not in existing.stderr.lower():
                raise RuntimeError('Cloud Run ownership preflight failed: ' + existing.stderr[:200])
        port_env = "8080"  # cloud run use PORT=8080
        base = f"FC_SERVER_PORT={port_env}" if self.typ == "function" else f"ATSUITE_MCP_PORT={port_env}"
        gcp_storage_bucket = os.environ.get("GCP_BUCKET", os.environ.get("GOOGLE_CLOUD_PROJECT", self.project_id))
        env_vars = f"{base},PROVIDER=gcp,PROJECT={gcp_storage_bucket},GOOGLE_CLOUD_PROJECT={self.project_id}"
        environment = dict(self.runtime_options.get('environment', {}))
        if any(',' in str(value) or '=' in str(key) or ',' in str(key) for key, value in environment.items()):
            raise ValueError('environment values must not contain gcloud separators')
        if environment:
            env_vars += ',' + ','.join(f'{key}={value}' for key, value in sorted(environment.items()))
        # Cloud Run request timeout is currently capped at 3600 seconds.
        effective_timeout = min(max(int(self.timeout), 1), _CLOUD_RUN_MAX_TIMEOUT_SECONDS)
        deploy_cmd = [
            "gcloud", "run", "deploy", service_name,
            "--image", image_uri,
            "--region", self.region,
            "--platform", "managed",
            "--project", self.project_id,
            "--memory", f"{self.memory_size}Mi",
            "--cpu", str(self.cpu),
            "--concurrency", str(self.runtime_options.get('concurrency', 1 if self.cpu < 1 else 50)),
            "--timeout", str(effective_timeout),
            "--set-env-vars", env_vars,
        ]
        deploy_cmd.append('--no-allow-unauthenticated' if self.strict_experiment else '--allow-unauthenticated')
        if self.ownership:
            deploy_cmd.extend(['--labels', 'atsuite-ownership=' + self.ownership])
        billing = self.runtime_options.get('billing_mode')
        if billing:
            if billing not in ('request_based', 'instance_based'):
                raise ValueError('unknown Cloud Run billing mode')
            deploy_cmd.append('--cpu-throttling' if billing == 'request_based' else '--no-cpu-throttling')
        environment_mode = self.runtime_options.get('execution_environment')
        if environment_mode:
            deploy_cmd.extend(['--execution-environment', environment_mode])
        # Keep one warm instance for both function and MCP services on GCP.
        deploy_cmd.extend(
            ["--min-instances", str(self.min_instances if self.min_instances is not None else 1)]
        )
        # MCP services additionally require sticky sessions.
        if self.typ == "mcp" and self.session_affinity is not False:
            deploy_cmd.append("--session-affinity")
        elif self.session_affinity is False:
            deploy_cmd.append('--no-session-affinity')
        run(deploy_cmd)

        url = self._get_service_url(service_name)  # service_name 已为 Cloud Run 合法名
        print("\n\nSuccess deploy on GCP Cloud Run\n\n")
        return url

    def _get_service_url(self, service_name: str) -> str:
        out = subprocess.run(
            [
                "gcloud", "run", "services", "describe", service_name,
                "--region", self.region,
                "--format", "value(status.url)",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip().rstrip("/")
