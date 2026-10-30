"""Cloud capacity readback; configured quotas are never physical instance counts."""

import json
import time
from dataclasses import asdict, dataclass

from atsuite.provider_model.observation import BACKENDS
from atsuite.workload.profile import number


def count(value: int | None, *, optional: bool = False) -> None:
    """Validate a nonnegative integer at the control boundary."""
    if optional and value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Capacity must be a nonnegative integer")


def session_retention_s(pool: dict) -> float:
    """Conservative remote residency after HTTP close; no Session-delete ACK exists."""
    provider, resources = pool["provider"], pool["resources"]
    if BACKENDS[provider][0] != "state-coupled":
        return 0
    if provider == "aws_agentcore":
        value = resources.get("lifecycle_configuration", {}).get("maxLifetime")
    else:
        affinity = resources.get("session_affinity_config") or {}
        if isinstance(affinity, str):
            affinity = json.loads(affinity)
        value = {k.lower(): v for k, v in affinity.items()}.get("sessionttlinseconds")
    if not number(value) or value == 0:
        raise ValueError(
            "State-coupled capacity requires its actual maximum Session lifetime"
        )
    return value


@dataclass(frozen=True)
class Capacity:
    """One readback, with unknown physical capacity represented by None."""

    observed_ns: int
    control: str
    target: int | None
    effective: int | None
    provisioned: int | None = None
    available: int | None = None
    actual_instances: int | None = None
    concurrency_quota: int | None = None
    status: str = "READY"

    def __post_init__(self):
        count(self.observed_ns)
        for field in (
            "target",
            "effective",
            "provisioned",
            "available",
            "actual_instances",
            "concurrency_quota",
        ):
            count(getattr(self, field), optional=True)

    def to_dict(self) -> dict:
        """Return a JSON-safe record for control logs."""
        return asdict(self)


class ScalingAdapter:
    """Use the existing provider clients; never update code or Session lifetimes.

    One controller must exclusively own the function/runtime. FC minimums and
    Lambda provisioned concurrency are warm floors, not caps on on-demand work.
    Session admission limits must be measured separately for each configuration.
    """

    def __init__(self, provider: str, client, resource: str, *, qualifier=None):
        if provider not in BACKENDS or not resource:
            raise ValueError("Unknown backend or empty resource")
        if qualifier == "$LATEST":
            raise ValueError("Lambda provisioned concurrency requires an alias/version")
        self.provider, self.client, self.resource = provider, client, resource
        self.qualifier = qualifier
        self.configuration = {}
        self.control = (
            "managed"
            if provider == "aws_agentcore"
            else "provisioned_concurrency"
            if provider == "aws_lambda" and qualifier
            else "reserved_concurrency"
            if provider == "aws_lambda"
            else "min_instances"
        )

    @classmethod
    def from_deployment(cls, provider: str, deployment, *, qualifier=None):
        """Reuse ATSuite's already constructed deployment adapter and SDK client."""
        if provider == "aws_agentcore":
            return cls(provider, deployment.agentcore_client, deployment._runtime_id)
        client = (
            deployment.lambda_client if provider == "aws_lambda" else deployment.client
        )
        return cls(provider, client, deployment.function_name, qualifier=qualifier)

    def read(self, now_ns: int | None = None) -> Capacity:
        """Read control-plane state; propagate authentication and service errors."""
        now = time.time_ns() if now_ns is None else now_ns
        if self.provider == "aws_agentcore":
            value = self.client.get_agent_runtime(agentRuntimeId=self.resource)
            if value.get("capacityProviderConfiguration"):
                raise ValueError(
                    "Instances compute is outside the audited microVM deployment"
                )
            self.configuration = {
                "runtime_version": value.get("agentRuntimeVersion"),
                "lifecycle_configuration": value.get("lifecycleConfiguration"),
            }
            return Capacity(now, self.control, None, None, status=value["status"])
        if self.provider == "aws_lambda":
            config = self.client.get_function_configuration(
                FunctionName=self.resource,
                **({"Qualifier": self.qualifier} if self.qualifier else {}),
            )
            self.configuration = {
                "memory_mb": config.get("MemorySize"),
                "timeout_s": config.get("Timeout"),
                "architectures": config.get("Architectures"),
                "ephemeral_storage": config.get("EphemeralStorage"),
            }
            status = (
                "READY"
                if config.get("State") == "Active"
                and config.get("LastUpdateStatus") == "Successful"
                else "UNAVAILABLE"
            )
            quota = self.client.get_function_concurrency(
                FunctionName=self.resource
            ).get("ReservedConcurrentExecutions")
            if self.control == "reserved_concurrency":
                return Capacity(
                    now,
                    self.control,
                    quota,
                    quota,
                    concurrency_quota=quota,
                    status=status,
                )
            try:
                value = self.client.get_provisioned_concurrency_config(
                    FunctionName=self.resource, Qualifier=self.qualifier
                )
            except self.client.exceptions.ProvisionedConcurrencyConfigNotFoundException:
                return Capacity(
                    now,
                    self.control,
                    0,
                    0,
                    0,
                    0,
                    concurrency_quota=quota,
                    status=status,
                )
            target = value["RequestedProvisionedConcurrentExecutions"]
            ready = (
                value["Status"] == "READY"
                and value["AllocatedProvisionedConcurrentExecutions"] >= target
            )
            allocation_status = value["Status"]
            if allocation_status == "READY" and not ready:
                allocation_status = "IN_PROGRESS"
            return Capacity(
                now,
                self.control,
                target,
                target if ready else None,
                value["AllocatedProvisionedConcurrentExecutions"],
                value["AvailableProvisionedConcurrentExecutions"],
                concurrency_quota=quota,
                status=allocation_status if status == "READY" else status,
            )
        from alibabacloud_fc20230330 import models

        value = self.client.get_scaling_config(
            self.resource, models.GetScalingConfigRequest(qualifier=self.qualifier)
        ).body
        self._fc_config = value
        config = self.client.get_function(
            self.resource, models.GetFunctionRequest(qualifier=self.qualifier)
        ).body
        self.configuration = {
            "cpu": config.cpu,
            "memory_mb": config.memory_size,
            "disk_mb": config.disk_size,
            "timeout_s": config.timeout,
            "instance_concurrency": config.instance_concurrency,
            "session_affinity": config.session_affinity,
            "session_affinity_config": config.session_affinity_config,
            "instance_isolation_mode": config.instance_isolation_mode,
        }
        quota = self.client.get_concurrency_config(
            self.resource
        ).body.reserved_concurrency
        # Competing policies would make min_instances an ambiguous control input.
        if value.horizontal_scaling_policies or value.scheduled_policies:
            raise ValueError("FC already has an independent scaling policy")
        target = value.min_instances
        current = value.current_instances
        ready = (
            target is not None
            and current is not None
            and current >= target
            and not value.current_error
            and config.state == "Active"
            and config.last_update_status == "Successful"
        )
        return Capacity(
            now,
            self.control,
            target,
            target if ready else None,
            provisioned=current,
            concurrency_quota=quota,
            status="FAILED"
            if value.current_error
            else "READY"
            if ready
            else "IN_PROGRESS",
        )

    def apply(self, target: int, *, active_sessions: int) -> None:
        """Submit an absolute target; a later read must confirm its effect.

        Without per-instance draining, reducing a pool waits for *all* Sessions.
        This also protects FaaS Sessions between Tool calls from quota reduction.
        """
        count(target)
        count(active_sessions)
        if self.control == "managed":
            raise ValueError("This runtime has platform-managed capacity")
        current = self.read()
        if current.target is None:
            raise ValueError("Unknown current target; cannot safely change capacity")
        if target < current.target and active_sessions:
            raise ValueError("Drain active Sessions before reducing capacity")
        if target == current.target:
            return
        if self.control == "reserved_concurrency":
            self.client.put_function_concurrency(
                FunctionName=self.resource, ReservedConcurrentExecutions=target
            )
        elif self.control == "provisioned_concurrency":
            if target:
                self.client.put_provisioned_concurrency_config(
                    FunctionName=self.resource,
                    Qualifier=self.qualifier,
                    ProvisionedConcurrentExecutions=target,
                )
            else:
                self.client.delete_provisioned_concurrency_config(
                    FunctionName=self.resource, Qualifier=self.qualifier
                )
        else:
            from alibabacloud_fc20230330 import models

            self.client.put_scaling_config(
                self.resource,
                models.PutScalingConfigRequest(
                    qualifier=self.qualifier,
                    body=models.PutScalingConfigInput(
                        min_instances=target,
                        **{
                            field: getattr(self._fc_config, field)
                            for field in (
                                "enable_mix_mode",
                                "enable_on_demand_scaling",
                                "horizontal_scaling_policies",
                                "request_dispatch_policy",
                                "resident_pool_id",
                                "scheduled_policies",
                            )
                        },
                    ),
                ),
            )
