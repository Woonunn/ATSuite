"""Untrusted initialization runs in a resource-limited container, without credentials."""
import os
import subprocess
from pathlib import Path
from .publish import require_digest


def run_init(script,working_dir,data_path,*,image=None,runner=subprocess.run,timeout=300):
    image=image or os.environ.get('ATSUITE_INIT_IMAGE','')
    if '@' not in image: raise RuntimeError('ATSUITE_INIT_IMAGE must be a pinned repository@sha256 digest')
    require_digest(image.rsplit('@',1)[1])
    work=Path(working_dir).resolve(); data=Path(data_path).resolve()
    script=Path(script).resolve()
    if work not in script.parents: raise ValueError('init script escapes builder context')
    command=['docker','run','--rm','--network','none','--cap-drop','ALL','--security-opt','no-new-privileges',
        '--memory','512m','--cpus','1','--pids-limit','128','--read-only','--tmpfs','/tmp:size=64m',
        '--mount',f'type=bind,src={work},dst=/work','--mount',f'type=bind,src={data},dst=/data,readonly',
        '--workdir','/work','--env','ATSUITE_DATA_PATH=/data',image,'/bin/bash',f'/work/{script.relative_to(work)}','/work']
    return runner(command,check=True,timeout=timeout,capture_output=True,text=True)
