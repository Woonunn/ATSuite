"""Resolve mutable provider-owned base tags once before dispatch; builds use digests."""
import json
import subprocess
from .publish import require_digest


def freeze_base_image(reference, platform, *, runner=subprocess.run):
    if '@' in reference:
        require_digest(reference.rsplit('@',1)[1]);return reference
    response=runner(['docker','buildx','imagetools','inspect',reference,'--format','{{json .Manifest}}'],
        check=True,capture_output=True,text=True,timeout=60)
    descriptor=json.loads(response.stdout)
    manifest_digest=require_digest(descriptor.get('digest'))
    repository=reference.rsplit(':',1)[0] if ':' in reference.rsplit('/',1)[-1] else reference
    pinned=f'{repository}@{manifest_digest}'
    response=runner(['docker','buildx','imagetools','inspect',pinned,'--raw'],check=True,capture_output=True,text=True,timeout=60)
    manifest=json.loads(response.stdout)
    entries=manifest.get('manifests',[])
    if entries:
        operating_system,architecture=platform.split('/')[:2]
        matches=[entry for entry in entries if entry.get('platform',{}).get('os')==operating_system and entry.get('platform',{}).get('architecture')==architecture]
        if len(matches)!=1:raise RuntimeError('base_manifest_platform_ambiguous_or_missing')
        pinned=f"{repository}@{require_digest(matches[0].get('digest'))}"
    return pinned


def freeze_candidate_bases(candidate, *, runner=subprocess.run):
    from pathlib import Path
    from atsuite.pipeline import resolve_benchmark
    from atsuite.cli.build_docker_images import normalize_python_version
    resolved=resolve_benchmark(Path(candidate['generated_config']),candidate['provider'])
    return {name:freeze_base_image(resolved.provider.base_image(normalize_python_version(target.build.python_version,candidate['provider'])),
                                 candidate['runtime_architecture'],runner=runner) for name,target in resolved.targets.items()}
