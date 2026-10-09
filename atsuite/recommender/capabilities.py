from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
from typing import Any, Protocol

from atsuite.pipeline import PROVIDERS
from atsuite.recommender.contracts import CandidateSpec
from atsuite.recommender.identity import digest


@dataclass(frozen=True)
class ResourceVector:
    cpu: float
    memory_mb: int
    disk_mb: int = 512
    timeout_s: int = 30
    architecture: str = 'linux/amd64'
    extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if isinstance(self.cpu, bool) or not isinstance(self.cpu, (float, int)) or not math.isfinite(self.cpu) or self.cpu <= 0:
            raise ValueError('cpu must be a positive finite number')
        for name in ('memory_mb', 'disk_mb', 'timeout_s'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f'{name} must be a positive integer')

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class ResourceConstraints:
    memory_values: tuple[int, ...] | None = None
    cpu_values: tuple[float, ...] | None = None
    architectures: tuple[str, ...] | None = None
    min_memory_mb: int = 128
    max_memory_mb: int = 10240
    disk_mb: int = 512
    timeout_s: int = 30


@dataclass(frozen=True)
class CapabilitySnapshot:
    snapshot_id: str
    provider: str
    region: str
    deployment_mode: str
    family: str
    source: str
    version: str
    cpu_mode: str
    memory_values: tuple[int, ...]
    cpu_values: tuple[float, ...]
    legal_cpu_memory: tuple[tuple[float, int], ...]
    architectures: tuple[str, ...]
    disk_values: tuple[int, ...]
    timeout_range: tuple[int, int]
    session_affinity: bool
    supports_concurrency: bool
    supports_effective_readback: bool
    tunable_fields: tuple[str, ...]
    billing_modes: tuple[str, ...]
    actual_service: str = ''
    memory_range: tuple[int, int, int] = (128, 10240, 1)
    cpu_range: tuple[float, float, float] = (1, 1, 1)
    disk_range: tuple[int, int, int] = (512, 512, 1)
    execution_environments: tuple[str, ...] = ()
    verified_at: str = '2026-10-08'
    rule_sources: tuple[str, ...] = ()
    resource_semantics: dict[str, str] = field(default_factory=dict)

    @property
    def supports_fractional_cpu(self):
        return any(cpu != int(cpu) for cpu in self.cpu_values)


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    reasons: tuple[str, ...] = ()
    effective: ResourceVector | None = None
    warnings: tuple[str, ...] = ()


class CapabilityProvider(Protocol):
    def describe(self, provider: str, region: str, deployment_mode: str = '') -> CapabilitySnapshot: ...
    def validate(self, candidate: CandidateSpec) -> ValidationResult: ...
    def enumerate_resources(self, constraints: ResourceConstraints, *, provider: str, region: str, deployment_mode: str = '') -> list[ResourceVector]: ...


def _gcp_pair(cpu, memory):
    if cpu < .08 or cpu > 8 or (cpu > 1 and cpu != int(cpu)):
        return False
    if cpu < 1 and not math.isclose(cpu * 1000, round(cpu * 1000), abs_tol=1e-7):
        return False
    if cpu < .5:
        return 128 <= memory <= 512
    if cpu < 1:
        return 128 <= memory <= 1024
    bounds = {1: (128, 4096), 2: (128, 8192), 3: (2048, 16384), 4: (2048, 16384),
              5: (4096, 24576), 6: (4096, 24576), 7: (4096, 32768), 8: (4096, 32768)}
    low, high = bounds[int(cpu)]
    return low <= memory <= high


class DefaultCapabilityProvider:
    """Versioned planning rules. Samples are separate from rule validation.

    No planning result is effective cloud readback. Managed service allocations
    stay unknown instead of being represented by invented fixed resources.
    """

    def describe(self, provider, region='', deployment_mode=''):
        provider = str(provider).lower().strip()
        spec = PROVIDERS.get(provider)
        if spec is None:
            raise ValueError(f'unknown provider: {provider}')
        mode = deployment_mode
        source = []
        if provider == 'aws_lambda':
            actual, default_mode = 'AWS Lambda', 'container_image'
            memory = (128, 256, 512, 1024, 2048, 4096, 8192, 10240)
            cpu = tuple(round(m / 1769, 3) for m in memory)
            pairs = tuple(zip(cpu, memory))
            cpu_mode, arch, tunable = 'derived', ('linux/amd64', 'linux/arm64'), ('memory', 'disk', 'timeout', 'architecture')
            memory_range, cpu_range, disk_range, timeout = (128, 10240, 1), (128 / 1769, 10240 / 1769, 0), (512, 10240, 1), (1, 900)
            billing, env = ('on_demand',), ('lambda',)
            source = ['https://docs.aws.amazon.com/lambda/latest/dg/configuration-memory.html', 'https://docs.aws.amazon.com/lambda/latest/dg/configuration-ephemeral-storage.html']
        elif provider.startswith('gcp'):
            actual, default_mode = 'Cloud Run', 'service_container'
            memory = (128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768)
            cpu = (.08, .25, .5, 1., 2., 4., 6., 8.)
            pairs = tuple((c, m) for c in cpu for m in memory if _gcp_pair(c, m))
            cpu_mode, arch, tunable = 'independent', ('linux/amd64',), ('cpu', 'memory', 'timeout', 'concurrency', 'billing_mode', 'execution_environment')
            memory_range, cpu_range, disk_range, timeout = (128, 32768, 1), (.08, 8, .001), (512, 512, 1), (1, 3600)
            billing, env = ('request_based', 'instance_based'), ('gen1', 'gen2')
            source = ['https://docs.cloud.google.com/run/docs/configuring/services/cpu', 'https://docs.cloud.google.com/run/docs/configuring/services/memory-limits']
        elif provider in {'ali_fc', 'ali_agentrun'}:
            actual, default_mode = ('Alibaba Function Compute', 'custom_container') if provider == 'ali_fc' else ('Alibaba Function Compute MCP session', 'custom_container_session')
            memory = (128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768)
            cpu = (.05, .1, .25, .5, 1., 2., 4., 8.)
            pairs = tuple((c, m) for c in cpu for m in memory if c * 1024 <= m <= c * 4096)
            cpu_mode, arch, tunable = 'independent', ('linux/amd64',), ('cpu', 'memory', 'disk', 'timeout')
            memory_range, cpu_range, disk_range, timeout = (128, 32768, 64), (.05, 16, .05), (512, 10240, 9728), (1, 86400)
            billing, env = ('on_demand',), ('custom_container',)
            source = ['https://www.alibabacloud.com/help/en/functioncompute/instance-types-and-specifications']
        else:
            actual, default_mode = {'aws_agentcore': ('Bedrock AgentCore Runtime', 'container_runtime'),
                'mcp_gateway': ('External MCP Gateway', 'external')}[provider]
            memory, cpu, pairs = (), (), ()
            cpu_mode = 'external' if provider == 'mcp_gateway' else 'provider_managed'
            arch = ('linux/arm64',) if provider == 'aws_agentcore' else ('linux/amd64',)
            tunable, memory_range, cpu_range, disk_range, timeout = (), (0, 0, 0), (0, 0, 0), (0, 0, 0), (1, 86400)
            billing, env = ('provider_managed',), ('provider_managed',)
            if provider == 'aws_agentcore':
                source = ['https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-how-it-works.html']
        # Legacy family names are accepted but normalized once to the actual
        # adapter deployment mode; they are never capability identities.
        mode = default_mode if mode in ('', 'faas', 'session') else mode
        if mode != default_mode:
            raise ValueError(f'unsupported deployment mode {mode!r} for {provider}')
        semantics = {'cpu': 'official_rule_derived' if cpu_mode == 'derived' else 'provider_managed_unknown' if not pairs else 'settable',
                     'memory': 'provider_managed_unknown' if not pairs else 'settable'}
        fields = dict(provider=provider, region=region or 'default', deployment_mode=mode, family=spec.family,
                      source='official_rules' if source else 'adapter_contract_unverified', version='2', cpu_mode=cpu_mode,
                      memory_values=memory, cpu_values=cpu, legal_cpu_memory=pairs, architectures=arch, disk_values=(512,),
                      timeout_range=timeout, session_affinity=spec.family == 'session', supports_concurrency=provider.startswith(('gcp', 'ali')),
                      supports_effective_readback=provider not in ('ali_agentrun', 'mcp_gateway'), tunable_fields=tunable,
                      billing_modes=billing, actual_service=actual, memory_range=memory_range, cpu_range=cpu_range,
                      disk_range=disk_range, execution_environments=env, rule_sources=tuple(source), resource_semantics=semantics)
        return CapabilitySnapshot('cap-' + digest(fields), **fields)

    def validate(self, candidate):
        snapshot = self.describe(candidate.provider, candidate.region, candidate.deployment_mode)
        reasons, warnings = [], []
        if not candidate.resources_by_target:
            reasons.append('missing_target_resources')
        for target, raw in candidate.resources_by_target.items():
            try:
                if 'cpu' not in raw or 'memory_mb' not in raw:
                    raise ValueError('missing cpu/memory; requested values cannot be imputed')
                resource = ResourceVector(raw['cpu'], raw['memory_mb'], raw.get('disk_mb', 512), raw.get('timeout_s', 30),
                    raw.get('architecture', candidate.runtime_architecture), raw.get('extras', {}))
            except (ValueError, TypeError) as exc:
                reasons.append(f'{target}: {exc}')
                continue
            cpu, memory = resource.cpu, resource.memory_mb
            if resource.architecture not in snapshot.architectures:
                reasons.append(f'{target}: unsupported_architecture')
            if snapshot.cpu_mode in {'provider_managed', 'external'}:
                warnings.append(f'{target}: provider_managed_resources_unknown; requested values are not effective allocation')
            elif candidate.provider == 'aws_lambda':
                if not 128 <= memory <= 10240 or not math.isclose(cpu, round(memory / 1769, 3), abs_tol=.0005):
                    reasons.append(f'{target}: invalid_memory_or_independently_tuned_derived_cpu')
            elif candidate.provider.startswith('gcp'):
                if not _gcp_pair(cpu, memory):
                    reasons.append(f'{target}: invalid_cloud_run_cpu_memory')
                extras = resource.extras
                if cpu < 1 and (extras.get('concurrency') != 1 or extras.get('billing_mode') != 'request_based' or extras.get('execution_environment') != 'gen1'):
                    reasons.append(f'{target}: fractional_cpu_requires_concurrency_1_request_based_gen1')
                if extras.get('execution_environment') == 'gen2' and memory < 512:
                    reasons.append(f'{target}: gen2_memory_below_512')
                if extras.get('billing_mode') not in (None, *snapshot.billing_modes):
                    reasons.append(f'{target}: invalid_billing_mode')
                if extras.get('execution_environment') not in (None, *snapshot.execution_environments):
                    reasons.append(f'{target}: invalid_execution_environment')
                concurrency = extras.get('concurrency')
                if concurrency is not None and (isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 1000):
                    reasons.append(f'{target}: invalid_concurrency')
            else:
                if (not 128 <= memory <= 32768 or memory % 64 or not .05 <= cpu <= 16
                    or not math.isclose(cpu / .05, round(cpu / .05), abs_tol=1e-7)
                    or not cpu * 1024 <= memory <= cpu * 4096 or resource.disk_mb not in (512, 10240)):
                    reasons.append(f'{target}: invalid_function_compute_resources')
            if snapshot.cpu_mode not in {'provider_managed', 'external'} and not snapshot.disk_range[0] <= resource.disk_mb <= snapshot.disk_range[1]:
                reasons.append(f'{target}: disk_outside_range')
            if not snapshot.timeout_range[0] <= resource.timeout_s <= snapshot.timeout_range[1]:
                reasons.append(f'{target}: timeout_outside_range')
        return ValidationResult(not reasons, tuple(reasons), None, tuple(warnings))

    def enumerate_resources(self, constraints, *, provider, region='', deployment_mode=''):
        snapshot = self.describe(provider, region, deployment_mode)
        memory_values = constraints.memory_values or snapshot.memory_values
        cpu_values = constraints.cpu_values or snapshot.cpu_values
        architectures = constraints.architectures or snapshot.architectures
        results = []
        pairs = ((round(m / 1769, 3), m) for m in memory_values) if snapshot.cpu_mode == 'derived' else ((c, m) for c in cpu_values for m in memory_values)
        for cpu, memory in pairs:
            if not constraints.min_memory_mb <= memory <= constraints.max_memory_mb:
                continue
            for architecture in architectures:
                extras = {'concurrency': 1, 'billing_mode': 'request_based', 'execution_environment': 'gen1'} if provider.startswith('gcp') and cpu < 1 else {}
                raw = ResourceVector(cpu, memory, constraints.disk_mb, constraints.timeout_s, architecture, extras)
                probe = CandidateSpec('probe', '', provider, snapshot.family, snapshot.deployment_mode, region, architecture, (), {'target': raw.to_dict()})
                if self.validate(probe).valid:
                    results.append(raw)
        return sorted(results, key=lambda value: (value.memory_mb, value.cpu, value.architecture))


_DEFAULT = DefaultCapabilityProvider()


def describe_capability(provider, region='', deployment_mode='', provider_impl=None):
    return (provider_impl or _DEFAULT).describe(provider, region, deployment_mode)


def enumerate_legal_resources(provider, region='', deployment_mode='', constraints=None, provider_impl=None):
    return (provider_impl or _DEFAULT).enumerate_resources(constraints or ResourceConstraints(), provider=provider, region=region, deployment_mode=deployment_mode)


def validate_candidate(candidate, provider_impl=None):
    return (provider_impl or _DEFAULT).validate(candidate)
