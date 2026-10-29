"""Seal environment-specific references without changing the original Capture."""
import json
from pathlib import Path

from atsuite.tracing.storage import read_text, trace_path
from experiments.exp1.data.prepare_e1b import ROOT, PROVIDERS, sha


def seal_reference(directory, original, task_id, provider):
    """Require independent validation and identical frozen decisions and programs."""
    events = [json.loads(line) for line in read_text(directory / 'events.jsonl').splitlines()]
    source = [json.loads(line) for line in read_text(original / 'events.jsonl').splitlines()]
    result = json.loads(read_text(directory / 'result.json'))
    if result.get('status') != 'complete' or result.get('validation', {}).get('status') != 'ok':
        raise ValueError('Environment reference failed independent result validation')
    decisions = lambda rows: [(e['phase'], e['iteration'], e['decision'], e['duration_s'])
                              for e in rows if e['kind'] == 'llm']
    programs = lambda rows: {e['key']: (e['tool_name'], e['args']) for e in rows
                            if e['kind'] == 'tool' and e['tool_name'] != 'merge_artifacts'}
    if decisions(events) != decisions(source) or programs(events) != programs(source):
        raise ValueError('Environment reference changed frozen decisions or Tool programs')
    starts = [e for e in events if e['kind'] == 'run_start']
    if (len(starts) != 1 or starts[0]['mode'] != 'environment_reference'
            or starts[0]['task_id'] != task_id or starts[0]['config']['tool']['provider'] != provider
            or Path(starts[0]['source_capture']).resolve() != original.resolve()):
        raise ValueError('Environment reference provenance mismatch')
    return {'capture': str(directory.resolve()),
            'files': {name: sha(trace_path(directory / name)) for name in ('events.jsonl', 'result.json')},
            'original_events_sha256': sha(trace_path(original / 'events.jsonl')),
            'independent_validation': result['validation']}


def load_references(path, manifest, deployment):
    """Validate every sealed candidate; a separate strict Pilot is still mandatory."""
    if path is None:
        return {}
    references = json.loads(Path(path).read_text())
    deployed = {e['provider']: e for e in json.loads(Path(deployment).read_text()).get('results', [])}
    if not references.get('deployments') and references['deployment_manifest_sha256'] != sha(deployment):
        raise ValueError('Environment references belong to another deployment')
    for provider, tasks in references['providers'].items():
        if provider not in PROVIDERS:
            raise ValueError('Unknown reference provider')
        if references.get('deployments') and (provider not in deployed or
                provider not in references['deployments'] or references['deployments'][provider] != deployed[provider]):
            raise ValueError('Environment reference provider deployment changed')
        for task_id, entry in tasks.items():
            original = ROOT / manifest['tasks'][task_id]['capture']
            if entry != seal_reference(Path(entry['capture']), original, task_id, provider):
                raise ValueError('Environment reference seal changed')
    return references['providers']
