"""One credential-free container and canonical Python heap per MCP Session."""
import atexit
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


def restrict_branch(out, result, done):
    # Landlock also prevents a branch from changing the resident command queue.
    import ctypes
    libc=ctypes.CDLL(None,use_errno=True)
    def call(number,*args):
        value=libc.syscall(number,*args)
        if value<0:raise OSError(ctypes.get_errno(),os.strerror(ctypes.get_errno()))
        return value
    abi=call(444,0,0,1)
    if abi<6:raise RuntimeError('Resident sandbox requires Landlock ABI >= 6 (filesystem and signal isolation)')
    class Ruleset(ctypes.Structure):
        _fields_=[('fs',ctypes.c_uint64),('net',ctypes.c_uint64),('scoped',ctypes.c_uint64)]
    class PathRule(ctypes.Structure):
        _pack_=1
        _fields_=[('access',ctypes.c_uint64),('fd',ctypes.c_int32)]
    read=(1<<0)|(1<<2)|(1<<3)
    write=(1<<1)|(1<<4)|(1<<5)|(1<<6)|(1<<7)|(1<<8)|(1<<12)|(1<<13)|(1<<14)
    ruleset=Ruleset((1<<16)-1,0,3)
    fd=call(444,ctypes.byref(ruleset),ctypes.sizeof(ruleset),0)
    try:
        for path,access in ((Path('/'),read),(out,read|write),(Path('/tmp'),read|write),(result,(1<<1)|(1<<14)),(done,(1<<1)|(1<<14)),(Path('/dev/null'),(1<<1))):
            target=os.open(path,os.O_PATH|os.O_CLOEXEC)
            try:
                rule=PathRule(access,target)
                call(445,fd,1,ctypes.byref(rule),0)
            finally:os.close(target)
        if libc.prctl(38,1,0,0,0):raise OSError(ctypes.get_errno(),'no_new_privs')
        call(446,fd,0)
    finally:os.close(fd)


def serve():
    import signal
    from sandbox_worker import run
    shared={};pending=None
    # Do not inherit native math worker threads across fork.
    import numpy,pandas
    signal.signal(signal.SIGCHLD,signal.SIG_IGN)
    root=Path('/session');(root/'ready').write_text(str(os.getpid()))
    while True:
        for queued in sorted((root/'queue').glob('*.json')):
            job=json.loads(queued.read_text());queued.unlink()
            out=Path(job['out']);request=job['request'];parent=Path(job['parent'])
            if request['mode'] in ('commit','abort'):
                valid=pending is not None and pending[0]==request['token']
                if valid:
                    if request['mode']=='commit':shared=pending[1]
                    pending=None
                result={'status':'ok'} if valid else {'status':'error','error_type':'ValueError','error':'Stale resident merge token'}
                (out.parent/'result.json').write_text(json.dumps(result));(out.parent/'done').write_text('1')
                continue
            args=(request,parent,out,Path('/inputs'),Path(job['branches']))
            if request['mode']=='merge':
                # Only the merge barrier changes the canonical resident heap.
                candidate=dict(shared)
                result=run(*args,shared=candidate,resident=True)
                if result['status']=='ok':
                    token=queued.stem;pending=(token,candidate);result['merge_token']=token
                    (out.parent/'result.json').write_text(json.dumps(result))
                (out.parent/'done').write_text('1')
                continue
            pid=os.fork()
            if pid==0:
                try:
                    os.setsid()
                    restrict_branch(out,out.parent/'result.json',out.parent/'done')
                    run(*args,shared=shared,resident=True)
                except BaseException as exc:
                    (out.parent/'result.json').write_text(json.dumps({'status':'error','error_type':type(exc).__name__,'error':str(exc)}))
                finally:
                    (out.parent/'done').write_text('1')
                    os._exit(0)
            (out.parent/'started.json').write_text(json.dumps({'pid':pid}))
        # Command transport polling; not synthetic benchmark computation.
        time.sleep(.01)


class ResidentKernel:
    def __init__(self,workspace,inputs,image):
        if not image.startswith('sha256:'):raise ValueError('Pin the sandbox image ID')
        self.workspace=Path(workspace).resolve();self.inputs=Path(inputs);self.name='wabber-mcp-'+uuid.uuid4().hex
        self.directory=Path(tempfile.mkdtemp(prefix='wabber-mcp-'));(self.directory/'queue').mkdir()
        self.image=image;self.closed=False;self.start_s=None
        command=['docker','run','-d','--name',self.name,'--network','none','--read-only',
            '--cpus','2','--memory','4g','--memory-swap','4g','--pids-limit','128',
            '--cap-drop','ALL','--security-opt','no-new-privileges','--security-opt','seccomp='+str(Path(__file__).with_name('seccomp.json')),'--user',f'{os.getuid()}:{os.getgid()}',
            '--ulimit','nofile=256:256','--ulimit','fsize=536870912:536870912','--log-driver','none',
            '--tmpfs','/tmp:rw,nosuid,size=256m','-v',f'{self.directory}:/session:rw',
            '-v',f'{self.workspace}:/snapshot:ro','-v',f'{Path(inputs).resolve()}:/inputs:ro',
            '--entrypoint','python',image,'/opt/wabber/resident_kernel.py','--serve']
        atexit.register(self.close)
        started=time.perf_counter()
        try:
            subprocess.run(command,check=True,capture_output=True,text=True)
            while not (self.directory/'ready').exists():
                if time.perf_counter()-started>30:raise TimeoutError('Resident kernel startup failed')
                time.sleep(.02)
            self.start_s=time.perf_counter()-started
            self.pid=int((self.directory/'ready').read_text())
        except BaseException:
            self.close();raise

    def close(self):
        if self.closed:return
        self.closed=True
        subprocess.run(['docker','rm','-f',self.name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        shutil.rmtree(self.directory,ignore_errors=True)

    def run(self,request,parent,inputs=None,branches=None):
        try:from .implementation import safe_files
        except ImportError:from implementation import safe_files
        if self.closed:raise RuntimeError('Resident kernel closed; create a new Session')
        job_id=uuid.uuid4().hex;job_root=self.directory/job_id;job_root.mkdir();out=job_root/'out';out.mkdir()
        result_path=job_root/'result.json';result_path.touch();done=job_root/'done';done.touch()
        mapped_parent='/snapshot/'+Path(parent).relative_to(self.workspace).as_posix()
        # Familiar input/parent filenames are read-only via branch Landlock policy.
        aliases={}
        for directory,mapped in ((self.inputs,'/inputs'),(Path(parent)/'artifacts',mapped_parent+'/artifacts')):
            for source in directory.rglob('*'):
                if source.is_file():
                    name=source.relative_to(directory).as_posix()
                    if name not in request.get('outputs',[]):aliases[name]=mapped+'/'+name
        aliases['inputs_dir']='/inputs';aliases['output_dir']='/session/'+job_id+'/out'
        for name,target in aliases.items():
            path=out/name;path.parent.mkdir(parents=True,exist_ok=True);path.symlink_to(target)
        if branches:shutil.copytree(branches,job_root/'branches')
        payload={'request':request,'out':'/session/'+job_id+'/out','parent':mapped_parent,'branches':'/session/'+job_id+'/branches'}
        temp=job_root/'command.json';temp.write_text(json.dumps(payload));temp.replace(self.directory/'queue'/(job_id+'.json'))
        started=time.perf_counter();checked=started
        try:
            while not done.stat().st_size:
                safe_files(out,aliases)
                if time.perf_counter()-checked>1:
                    state=subprocess.run(['docker','inspect','--format','{{json .State}}',self.name],capture_output=True,text=True)
                    if state.returncode or not json.loads(state.stdout)['Running']:
                        raise RuntimeError('Resident kernel stopped; create a new Session: '+state.stdout[:2000])
                    checked=time.perf_counter()
                if time.perf_counter()-started>float(os.getenv('WABBER_CODE_TIMEOUT','180')):
                    if (job_root/'started.json').exists():
                        pid=json.loads((job_root/'started.json').read_text())['pid']
                        subprocess.run(['docker','exec',self.name,'python','-c',f'import os,signal;os.killpg({int(pid)},signal.SIGKILL)'],check=True,capture_output=True)
                    else:self.close()
                    if done.exists():done.write_text('killed')
                    raise TimeoutError('Generated code exceeded sandbox time limit')
                time.sleep(.02)
            result=json.loads(result_path.read_text())
            result.update(sandbox_image=self.image,container_name=self.name,sandbox_wall_s=time.perf_counter()-started,
                          resident_kernel_pid=self.pid,kernel_reused=True)
            for name,target in aliases.items():
                p=out/name
                if not p.is_symlink() or os.readlink(p)!=target:raise ValueError('Sandbox input alias modified')
                p.unlink()
            safe_files(out)
            if result['status']!='ok' or request['mode'] in ('commit','abort'):return result,None
            import tarfile
            fd,name=tempfile.mkstemp(prefix='wabber-branch-',suffix='.tar');os.close(fd)
            with tarfile.open(name,'w') as archive:
                for p in safe_files(out):
                    relative=p.relative_to(out).as_posix()
                    if relative in {'state.pkl','stdout.txt',*request.get('outputs',[])}:archive.add(p,arcname=relative,recursive=False)
            return result,Path(name)
        finally:
            if not done.exists() or not done.stat().st_size:self.close()
            shutil.rmtree(job_root,ignore_errors=True)


if __name__=='__main__':serve()
