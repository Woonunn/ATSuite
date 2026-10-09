"""Registry publication is distinct from the local Docker image identity."""
from __future__ import annotations
import json
import re
import subprocess
from dataclasses import asdict,dataclass
from .artifacts import create_build_artifact

DIGEST=re.compile(r'^sha256:[a-f0-9]{64}$')

def require_digest(value):
    if not DIGEST.fullmatch(str(value)): raise ValueError('invalid registry digest')
    return value

@dataclass(frozen=True)
class PublishedArtifact:
    repository:str
    digest:str
    platform:str
    build_id:str
    local_image_id:str=''
    registry_verified:bool=True
    @property
    def image_ref(self): return f'{self.repository}@{self.digest}'


def publish_artifact(artifact,repository,*,runner=subprocess.run):
    if '/' not in repository or '@' in repository: raise ValueError('registry repository required')
    tag=f"{repository}:{artifact['build_id']}"
    runner(['docker','tag',artifact['repository'],tag],check=True,capture_output=True,text=True)
    runner(['docker','push',tag],check=True,capture_output=True,text=True)
    response=runner(['docker','buildx','imagetools','inspect',tag,'--format','{{json .Manifest}}'],check=True,capture_output=True,text=True)
    manifest=json.loads(response.stdout)
    remote_digest=require_digest(manifest.get('digest'))
    image=f'{repository}@{remote_digest}'
    descriptor=runner(['docker','buildx','imagetools','inspect',image,'--raw'],check=True,capture_output=True,text=True)
    raw=json.loads(descriptor.stdout)
    manifests=raw.get('manifests',[])
    platform=artifact['platform']
    os_name,arch=platform.split('/')[:2]
    if manifests and not any(m.get('platform',{}).get('os')==os_name and m.get('platform',{}).get('architecture')==arch for m in manifests):
        raise RuntimeError('registry manifest platform mismatch')
    # Pull by digest checks current registry access and concrete single-image platform.
    runner(['docker','pull','--platform',platform,image],check=True,capture_output=True,text=True)
    local=runner(['docker','image','inspect',image],check=True,capture_output=True,text=True)
    details=json.loads(local.stdout)[0]
    if details.get('Os')!=os_name or details.get('Architecture')!=arch: raise RuntimeError('published image architecture mismatch')
    return {**artifact,**asdict(PublishedArtifact(repository,remote_digest,platform,artifact['build_id'],details.get('Id',''))),
            'digest_verified':True,'registry_manifest':manifest}
