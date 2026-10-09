import os
import re
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from atsuite.pipeline import CliOverrides, is_sandbox_config, logical_state_domain, resolve_benchmark, target_image_tag
from atsuite.runtime import GatewayClient
from atsuite.utils import write_url_map


def _artifact_dict(artifact: Any | None) -> dict[str, Any] | None:
    if artifact is None:
        return None
    if is_dataclass(artifact):
        return asdict(artifact)
    if isinstance(artifact, dict):
        return dict(artifact)
    raise TypeError("deployment artifact must be a BuildArtifact or mapping")


def _artifact_image_ref(artifact: Any | None) -> str | None:
    data = _artifact_dict(artifact)
    if data is None:
        return None
    repository = str(data.get("repository") or "").strip()
    digest = str(data.get("digest") or "").strip()
    if not repository or not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
        raise RuntimeError("Deployment artifact requires repository and verified sha256 digest")
    repository = repository.split("@", 1)[0]
    leaf = repository.rsplit("/", 1)[-1]
    if ":" in leaf:
        repository = repository.rsplit(":", 1)[0]
    return f"{repository}@{digest}"


def _deployment_result(value: Any, requested: dict[str, Any]) -> tuple[str, dict[str, Any] | None, str]:
    if isinstance(value, str):
        return value, None, "unverified"
    if isinstance(value, dict):
        endpoint = str(value.get("endpoint") or value.get("url") or "")
        effective = value.get("effective_resources") or value.get("effective_config")
        if effective is not None:
            effective = dict(effective)
            mismatches = {
                key: (requested.get(key), value)
                for key, value in effective.items()
                if key in requested and requested[key] != value
            }
            if mismatches:
                raise RuntimeError(
                    f"deployed resource configuration differs from request: "
                    f"requested={requested}, effective={effective}"
                )
            readback = str(value.get("configuration_readback") or "")
            if readback not in {"verified", "partial", "unverified"}:
                readback = "verified" if set(effective) == set(requested) else "partial"
            elif readback == "verified" and set(effective) != set(requested):
                readback = "partial"
            return endpoint, effective, readback
        return endpoint, None, str(value.get("configuration_readback") or "unverified")
    if value is None:
        return "", None, "unverified"
    raise TypeError("deployer result must be an endpoint string or mapping")


def deploy(
    config_path: Path,
    provider: str,
    *,
    runtime: Optional[str] = None,
    artifacts: Optional[Dict[str, Any]] = None,
    experiment_id: str = "",
    candidate_id: str = "",
    output_dir: Path | None = None,
    namespace: str = "",
    region: str = "",
    on_resource: Any = None,
    ownership: str = "",
    tenant: str = "",
    state_bucket: str = "",
) -> Optional[Path]:
    if is_sandbox_config(config_path):
        raise SystemExit("Sandbox configs are no longer supported; use an external MCP-Gateway target instead")

    resolved = resolve_benchmark(config_path, provider, CliOverrides())
    if artifacts is not None and set(artifacts) != set(resolved.targets):
        raise RuntimeError('artifact map must cover every target exactly')
    endpoint_cache: Dict[str, Dict[str, object]] = {}

    if provider == "mcp_gateway":
        gateway = GatewayClient()
        for target in resolved.targets.values():
            print(f"[deploy] Registering gateway target: '{target.name}'")
            manifest = {"allowed_tools": list(_allowed_tools_for_target(resolved.nodes, target))}
            artifact = _artifact_dict((artifacts or {}).get(target.name))
            image_ref = _artifact_image_ref(artifact) if artifact else _gateway_image_ref(resolved.bench_name, target)
            result = gateway.register_target(name=target.name, image=image_ref, resources=target.runtime.to_dict(), manifest=manifest)
            endpoint, effective, readback = _deployment_result(result, target.runtime.to_dict())
            endpoint_cache[target.name] = _endpoint_entry(endpoint, target, manifest, artifact=artifact, effective=effective, readback=readback)
        return _write_map(config_path, provider, resolved.family, endpoint_cache, experiment_id, candidate_id, output_dir)

    function_deployer = None
    mcp_deployer = None
    if resolved.family == "faas":
        if provider == "ali_fc":
            from atsuite.faas.function import AliFunctionDeployer
            function_deployer = AliFunctionDeployer(resolved.bench_name, region=region)
        elif provider == "aws_lambda":
            from atsuite.faas.function import AWSFunctionDeployer
            function_deployer = AWSFunctionDeployer(resolved.bench_name, region=region)
        elif provider == "gcp_faas":
            from atsuite.faas.function import GCPFunctionDeployer
            function_deployer = GCPFunctionDeployer(resolved.bench_name, region=region)
        else:
            raise SystemExit(f"Unsupported FaaS provider: {provider}")
    else:
        if provider == "ali_agentrun":
            from atsuite.mcp.mcp import AliMCPDeployer
            mcp_deployer = AliMCPDeployer(resolved.bench_name, region=region)
        elif provider == "aws_agentcore":
            from atsuite.mcp.mcp import AWSAgentCoreMCPDeployer
            mcp_deployer = AWSAgentCoreMCPDeployer(resolved.bench_name, region=region)
        elif provider == "gcp_mcp":
            from atsuite.mcp.mcp import GCPMCPDeployer
            mcp_deployer = GCPMCPDeployer(resolved.bench_name, region=region)
        else:
            raise SystemExit(f"Unsupported MCP provider: {provider}")

    for target in resolved.targets.values():
        artifact = _artifact_dict((artifacts or {}).get(target.name))
        image_ref = _artifact_image_ref(artifact) if artifact else None
        label = "function" if target.family == "faas" else "mcp"
        print(f"[deploy] Deploying {label} target: '{target.name}'")
        deployer = function_deployer if target.family == "faas" else mcp_deployer
        from atsuite.recommender.identity import digest as identity_digest
        physical_name = ('at-' + identity_digest({'namespace':namespace, 'experiment':experiment_id,
            'candidate':candidate_id,'target':target.name},length=40)) if (namespace or experiment_id or candidate_id) else target.name
        intent_name = physical_name
        if provider == 'aws_agentcore': intent_name = 'atsuite_' + physical_name.lower().replace('-', '_')
        elif provider.startswith('ali_'): intent_name += '-function' if target.family == 'faas' else '-mcp'
        if on_resource:
            on_resource({'logical_target':target.name,'physical_name':intent_name,'status':'creating','provider':provider,'region':region})
        options={'strict_experiment':bool(experiment_id), 'ownership':ownership,
            'environment':{'ATSUITE_TENANT':tenant or 'local','ATSUITE_EXPERIMENT_ID':experiment_id,
                'ATSUITE_CANDIDATE_ID':candidate_id,'ATSUITE_SCENARIO_ID':'default',
                'ATSUITE_LOGICAL_STATE_DOMAIN':logical_state_domain(resolved, target.name),
                **({'PROJECT':state_bucket} if state_bucket else {}),
                **({'ALI_REGION':region} if provider.startswith('ali_') else {})},
            'architecture':target.runtime.architecture or artifacts.get(target.name,{}).get('platform','') if artifacts else target.runtime.architecture,
            'concurrency':target.runtime.concurrency,'billing_mode':target.runtime.billing_mode,'region':region}
        result = deployer.deploy_target(physical_name, target.runtime, image_ref=image_ref, runtime_options=options) if experiment_id else deployer.deploy_target(physical_name,target.runtime,image_ref=image_ref)
        requested = target.runtime.to_dict()
        if isinstance(result, str):
            result = {"endpoint": result}
        if hasattr(deployer, "readback_target"):
            try:
                details = deployer.readback_target(physical_name)
                if details:
                    result = {**result, **details}
            except Exception as exc:
                result = {**result, "configuration_readback": "unverified",
                          "readback_error": f"{type(exc).__name__}: {exc}"}
        endpoint, effective, readback = _deployment_result(result, requested)
        if on_resource:
            on_resource({'logical_target':target.name,'physical_name':physical_name,'status':'created',
                         'provider':provider,'region':region,'physical_name':str(result.get('physical_name') or physical_name),
                         'runtime_id':result.get('runtime_id',''), 'resource_type':result.get('resource_type',''),
                         'physical_resource_id':str(result.get('physical_resource_id') or physical_name)})
        if endpoint:
            endpoint_cache[target.name] = _endpoint_entry(
                endpoint.rstrip("/"), target,
                {"allowed_tools": list(_allowed_tools_for_target(resolved.nodes, target))},
                artifact=artifact, effective=effective, readback=readback,
                physical_resource_id=str(result.get("physical_resource_id") or ""),
                readback_error=str(result.get("readback_error") or ""),
            )
            endpoint_cache[target.name]['readback']={key:value for key,value in result.items() if key not in {'endpoint','url'}}

    return _write_map(config_path, provider, resolved.family, endpoint_cache, experiment_id, candidate_id, output_dir)


def _write_map(config_path: Path, provider: str, family: str, targets: dict[str, Any], experiment_id: str, candidate_id: str, output_dir: Path | None) -> Path:
    payload = {"provider": provider, "family": family, "targets": targets}
    output_path = write_url_map(config_path, payload, output_dir=output_dir, experiment_id=experiment_id, candidate_id=candidate_id)
    print(f"[deploy] URLs saved to {output_path}")
    print(f"[deploy] Deployed {len(targets)} target(s)")
    return output_path


def _endpoint_entry(url: str, target, manifest: Dict[str, object], artifact: Optional[Dict[str, Any]] = None,
                    effective: dict[str, Any] | None = None, readback: str = "unverified",
                    physical_resource_id: str = "", readback_error: str = "") -> Dict[str, object]:
    requested = target.runtime.to_dict()
    entry: Dict[str, object] = {
        "endpoint": url.rstrip("/"), "family": target.family, "resources": requested,
        "requested_resources": requested, "effective_resources": effective,
        "configuration_readback": readback, "tool_manifest": manifest,
        "physical_resource_id": physical_resource_id,
    }
    if readback_error:
        entry["readback_error"] = readback_error
    if artifact:
        entry["artifact"] = {
            "build_id": artifact.get("build_id", ""), "repository": artifact.get("repository", ""),
            "digest": artifact.get("digest", ""), "platform": artifact.get("platform", ""),
        }
    return entry


def _gateway_image_ref(bench_name: str, target) -> str:
    image = target_image_tag(bench_name, target)
    prefix = os.environ.get("MCP_GATEWAY_IMAGE_PREFIX", "").strip().rstrip("/")
    if prefix:
        return f"{prefix}/{image}"
    if "/" in image:
        return image
    raise RuntimeError("MCP_GATEWAY_IMAGE_PREFIX is required for mcp_gateway deploy so the Gateway can pull the image")


def _allowed_tools_for_target(nodes, target) -> tuple[str, ...]:
    if target.family == "faas":
        return tuple(target.allowed_tools)
    tools = []
    for node_name in target.node_names:
        node = nodes[node_name]
        for trace_name in node.trace_names:
            tools.append(node.tool_name_for_trace(trace_name))
    return tuple(dict.fromkeys(tools))
