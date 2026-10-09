"""Explicit production change transaction, separate from experimental deployment.

Adapters are server configured capabilities; API callers never supply modules or
commands. A failed health check restores the exact pre-change snapshot.
"""
from __future__ import annotations

from typing import Any, Protocol
from pathlib import Path
import json
from atsuite.recommender.identity import digest, file_digest


class ProductionAdapter(Protocol):
    def inspect(self, target: str) -> dict[str, Any]: ...
    def apply(self, target: str, config: dict[str, Any]) -> dict[str, Any]: ...
    def healthy(self, target: str) -> bool: ...
    def restore(self, target: str, snapshot: dict[str, Any]) -> dict[str, Any]: ...


def prepare_change(record: dict, candidate_id: str, target: str, adapter: ProductionAdapter,
                   *, tenant: str, project: str) -> dict:
    recommendation = record.get('recommendation') or {}
    if candidate_id != recommendation.get('best_verified') or recommendation.get('status') not in {'recommended', 'best_verified'}:
        raise ValueError('production apply requires the verified objective winner')
    candidate = next((c for c in record['plan'].get('candidates', []) if c['candidate_id'] == candidate_id), None)
    if candidate is None or not candidate.get('config_digest'):
        raise ValueError('verified candidate config digest is missing')
    snapshot = adapter.inspect(target)
    if snapshot.get('tenant') != tenant or snapshot.get('project') != project:
        raise PermissionError('target ownership does not match principal')
    config = candidate.get('recommended_config') or candidate.get('deployment_config')
    configuration_digest = candidate['config_digest']
    if isinstance(config, dict):
        if digest(config, length=64) != configuration_digest:
            raise ValueError('candidate configuration cannot be verified')
    else:
        path = Path(candidate.get('generated_config', ''))
        configuration_digest = candidate.get('generated_config_digest')
        if not path.is_file() or not configuration_digest or file_digest(path) != configuration_digest:
            raise ValueError('candidate frozen configuration cannot be verified')
        config = json.loads(path.read_text())
        deployments = record.get('deployment_records') or []
        deployment = next((row for row in deployments if row.get('candidate_id') == candidate_id), None)
        published = (record.get('published_artifacts') or {}).get(candidate_id)
        if not deployment or not published:
            raise ValueError('candidate immutable published image evidence is missing')
        config = {'benchmark_config': config, 'resources_by_target': candidate['resources_by_target'],
                  'published_artifacts': published}
    change = {'experiment_id': record['id'], 'candidate_id': candidate_id, 'target': target,
              'tenant': tenant, 'project': project, 'plan_digest': record['plan'].get('plan_digest'),
              'config_digest': configuration_digest, 'config': config, 'snapshot': snapshot,
              'snapshot_digest': digest(snapshot, length=64), 'status': 'dry_run'}
    change['deployment_plan_id'] = 'apply-' + digest(change, length=32)
    return change


def apply_change(change: dict, approval: dict, adapter: ProductionAdapter) -> dict:
    if approval.get('deployment_plan_id') != change['deployment_plan_id'] or approval.get('config_digest') != change['config_digest']:
        raise PermissionError('additional production approval does not match change')
    current = adapter.inspect(change['target'])
    if digest(current, length=64) != change['snapshot_digest']:
        raise ValueError('target drift detected; rebuild deployment plan and approval')
    result = {'deployment_plan_id': change['deployment_plan_id'], 'snapshot': change['snapshot'],
              'config_digest': change['config_digest']}
    try:
        result['effective'] = adapter.apply(change['target'], change['config'])
        if not adapter.healthy(change['target']):
            raise RuntimeError('production health check failed')
        result.update(status='applied', health_check='passed')
    except Exception as exc:
        result.update(status='rolled_back', error=f'{type(exc).__name__}: {exc}',
                      rollback=adapter.restore(change['target'], change['snapshot']))
        if adapter.inspect(change['target']) != change['snapshot']:
            result.update(status='rollback_failed', rollback_error='snapshot readback mismatch')
    return result


class AWSLambdaProductionAdapter:
    """Built-in adapter for server-allowlisted, existing Lambda targets.

    This deliberately requires digest-published images and never creates a new
    function, changes IAM permissions, or discovers arbitrary caller ARNs.
    """
    def __init__(self, targets: dict[str, dict], *, client=None):
        self.targets = dict(targets)
        self._client = client

    def _target(self, target):
        if target not in self.targets:
            raise PermissionError('target is not in server production allowlist')
        row = self.targets[target]
        if not row.get('function_name') or not row.get('tenant') or not row.get('project'):
            raise ValueError('production target scope is incomplete')
        return row

    def _api(self, target):
        row = self._target(target)
        if self._client is not None:
            return self._client
        import boto3
        return boto3.client('lambda', region_name=row['region'])

    def inspect(self, target):
        row = self._target(target)
        api = self._api(target)
        response = api.get_function(FunctionName=row['function_name'])
        config, code = response['Configuration'], response['Code']
        tags = api.list_tags(Resource=config['FunctionArn']).get('Tags', {})
        if tags.get('atsuite:tenant') != row['tenant'] or tags.get('atsuite:project') != row['project']:
            raise PermissionError('provider ownership tags do not match target allowlist')
        image = code.get('ResolvedImageUri')
        if not image or '@sha256:' not in image:
            raise ValueError('production target requires immutable image readback')
        return {'tenant': row['tenant'], 'project': row['project'], 'function_name': row['function_name'],
                'image_uri': image, 'memory_mb': config['MemorySize'], 'timeout_s': config['Timeout'],
                'disk_mb': config.get('EphemeralStorage', {}).get('Size', 512),
                'architecture': config.get('Architectures', ['x86_64'])[0]}

    def _write(self, target, values):
        row = self._target(target)
        api = self._api(target)
        api.update_function_code(FunctionName=row['function_name'], ImageUri=values['image_uri'])
        api.get_waiter('function_updated').wait(FunctionName=row['function_name'])
        api.update_function_configuration(FunctionName=row['function_name'], MemorySize=int(values['memory_mb']),
                                          Timeout=int(values['timeout_s']), EphemeralStorage={'Size': int(values['disk_mb'])})
        api.get_waiter('function_updated').wait(FunctionName=row['function_name'])
        effective = self.inspect(target)
        for key in ('image_uri', 'memory_mb', 'timeout_s', 'disk_mb'):
            if effective[key] != values[key]:
                raise RuntimeError(f'production readback mismatch: {key}')
        return effective

    def apply(self, target, config):
        resources = config.get('resources_by_target') or {}
        images = config.get('published_artifacts') or {}
        if len(resources) != 1 or len(images) != 1:
            raise ValueError('Lambda production target requires exactly one deploy unit')
        resource, image = next(iter(resources.values())), next(iter(images.values()))
        repository = image.get('repository', '')
        image_digest = image.get('digest', '')
        if not repository or not image_digest.startswith('sha256:') or len(image_digest) != 71:
            raise ValueError('production image digest is invalid')
        architecture = {'linux/amd64': 'x86_64', 'linux/arm64': 'arm64'}.get(resource.get('architecture'))
        if architecture is None or self.inspect(target)['architecture'] != architecture:
            raise ValueError('production architecture change requires a separately approved target migration')
        from atsuite.deploy import _artifact_image_ref
        return self._write(target, {'image_uri': _artifact_image_ref(image),
                                   'memory_mb': resource['memory_mb'], 'timeout_s': resource['timeout_s'],
                                   'disk_mb': resource['disk_mb']})

    def healthy(self, target):
        row = self._target(target)
        if 'health_payload' not in row or 'health_expected' not in row:
            raise ValueError('server target must configure a deterministic health oracle')
        response = self._api(target).invoke(FunctionName=row['function_name'], InvocationType='RequestResponse',
                                            Payload=json.dumps(row['health_payload']).encode())
        payload = json.loads(response['Payload'].read())
        return not response.get('FunctionError') and response.get('StatusCode') == 200 and payload == row['health_expected']

    def restore(self, target, snapshot):
        return self._write(target, snapshot)
