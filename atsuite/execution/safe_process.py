"""Builder runs as a credential-free child process; uploaded init runs inside containers."""
import json
import os
import subprocess
import sys
from pathlib import Path

_ALLOWED={'PATH','TMPDIR','LANG','LC_ALL','DOCKER_HOST','DOCKER_CONTEXT','ATSUITE_INIT_IMAGE','ATSUITE_PIP_INDEX_URL','ATSUITE_PIP_TRUSTED_HOST'}

def run_builder(candidate,output,*,experiment_id,build_id):
    env={k:v for k,v in os.environ.items() if k in _ALLOWED}
    # No cloud/API tokens or credential-bearing Docker config in the builder.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='atsuite-builder-') as config:
        env['DOCKER_CONFIG']=config
        env['HOME']=config
        manifest=Path(output)/experiment_id/(build_id+'.json')
        manifest.parent.mkdir(parents=True,exist_ok=True)
        command=[sys.executable,'-m','atsuite.execution.safe_process','--config',candidate['generated_config'],'--provider',candidate['provider'],
            '--output',str(output),'--platform',candidate['runtime_architecture'],'--experiment',experiment_id,'--build',build_id,'--manifest',str(manifest),
            '--base-images',json.dumps(candidate.get('base_images'))]
        import signal
        process=subprocess.Popen(command,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
        try:
            stdout,stderr=process.communicate(timeout=1800)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGTERM)
            try: process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL);process.communicate()
            raise
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode,command,output=stdout,stderr=stderr)
        return json.loads(manifest.read_text())

def main():
    import argparse
    from atsuite.cli.build_docker_images import build_resolved_targets
    p=argparse.ArgumentParser();p.add_argument('--config');p.add_argument('--provider');p.add_argument('--output');p.add_argument('--platform');p.add_argument('--experiment');p.add_argument('--build');p.add_argument('--manifest');p.add_argument('--base-images');a=p.parse_args()
    result=build_resolved_targets(Path(a.config),a.provider,a.output,platform=a.platform,experiment_id=a.experiment,candidate_id=a.build,base_images=json.loads(a.base_images))
    Path(a.manifest).write_text(json.dumps(result))
if __name__=='__main__': main()
