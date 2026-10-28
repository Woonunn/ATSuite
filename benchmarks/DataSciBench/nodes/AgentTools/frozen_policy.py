"""Closed-program replay admission and file guards; not a general Python sandbox."""
import hashlib
import json
import os
from pathlib import Path
import sys


def policy():
    path=os.environ.get('WABBER_APPROVED_TRACE')
    if not path:raise ValueError('Frozen replay requires an image-baked approved Trace')
    return json.loads(Path(path).read_text())


def check_preparation(task_id,dataset_refs):
    approved=policy()
    if task_id!=approved['task_id'] or dataset_refs!=approved['dataset_refs']:
        raise ValueError('Frozen replay accepts only its approved task and dataset version')


def check_analysis(task_id,step_id,code,outputs,snapshot,seed):
    approved=policy()
    actual={'step_id':step_id,'code_sha256':hashlib.sha256(code.encode()).hexdigest(),'outputs':outputs,'snapshot':snapshot,'seed':seed}
    if task_id!=approved['task_id'] or actual not in approved['analyses']:
        raise ValueError('Unapproved code or parameters: cloud replay never accepts new generated Python')


def file_guard(readable,writable):
    # The approved, fixed program is the trust boundary; audit hooks alone cannot sandbox arbitrary Python.
    def paths(values):return [(Path(p).resolve(),Path(p).is_dir()) for p in values if Path(p).exists()]
    read=paths(readable+writable);write=paths(writable)
    def allowed(path,roots):
        if isinstance(path,int):return True
        resolved=Path(os.fsdecode(path)).resolve()
        return any(resolved==root or directory and resolved.is_relative_to(root) for root,directory in roots)
    def require(path,roots):
        if not allowed(path,roots):raise PermissionError('Frozen Trace file policy rejected: '+os.fsdecode(path))
    def audit(event,args):
        if event=='open':
            mode,flags=args[1],args[2]
            writing=bool(flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND)) if isinstance(flags,int) else bool(mode and any(c in mode for c in 'wa+x'))
            require(args[0],write if writing else read)
        elif event in ('os.listdir','os.scandir','os.chdir'):
            require(args[0] if args and args[0] is not None else os.getcwd(),read)
        elif event in ('os.remove','os.rmdir','os.mkdir','os.chmod','os.chown','os.truncate'):
            require(args[0],write)
        elif event in ('os.rename','os.link','os.symlink'):
            require(args[0],write);require(args[1],write)
        elif event in ('os.system','os.kill','os.killpg','os.fork','os.posix_spawn') or event.startswith(('socket.','subprocess.','os.exec','os.spawn')):
            raise PermissionError('Frozen Trace does not permit network or subprocess operations')
    sys.addaudithook(audit)
