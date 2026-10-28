"""Provider-local sandbox: resident MCP heap; one restored worker per FaaS call."""
import atexit
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import select
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid


def restrict_process(readable, writable, *, branch=False, landlock=True, platform_seccomp=False):
    """Fail closed if the kernel cannot enforce filesystem and syscall isolation."""
    libc = ctypes.CDLL(None, use_errno=True)
    def syscall(number, *args):
        value = libc.syscall(number, *args)
        if value < 0:
            raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
        return value
    class CapHeader(ctypes.Structure):
        _fields_=[('version',ctypes.c_uint32),('pid',ctypes.c_int)]
    class CapData(ctypes.Structure):
        _fields_=[('effective',ctypes.c_uint32),('permitted',ctypes.c_uint32),('inheritable',ctypes.c_uint32)]
    header=CapHeader(0x20080522,0);capabilities=(CapData*2)()
    if libc.capget(ctypes.byref(header),ctypes.byref(capabilities)):
        raise OSError(ctypes.get_errno(),'read process capabilities')
    if any(c.effective or c.permitted or c.inheritable for c in capabilities):
        if libc.capset(ctypes.byref(header),ctypes.byref((CapData*2)())):
            raise OSError(ctypes.get_errno(),'drop process capabilities')
        if libc.capget(ctypes.byref(header),ctypes.byref(capabilities)) or any(c.effective or c.permitted or c.inheritable for c in capabilities):
            raise PermissionError('Process capabilities were not cleared')
    if platform_seccomp:
        if landlock:raise ValueError('Platform-only isolation is limited to frozen programs')
        status=process_status()
        if status.get('Seccomp')!='2' or any(status.get(k)!='0000000000000000' for k in ('CapEff','CapPrm','CapInh')):
            raise PermissionError('Lambda closed replay requires inherited platform seccomp and zero capabilities')
        return None
    if libc.prctl(38,1,0,0,0):raise OSError(ctypes.get_errno(),'no_new_privs')
    abi=None
    if landlock:
        abi = syscall(444, 0, 0, 1)
        if abi < 1:
            raise RuntimeError('Landlock is required for the native Tool sandbox')
        class Ruleset(ctypes.Structure):
            _fields_ = [('fs', ctypes.c_uint64)]
        class PathRule(ctypes.Structure):
            _pack_ = 1
            _fields_ = [('access', ctypes.c_uint64), ('fd', ctypes.c_int32)]
        handled = (1 << (15 if abi >= 3 else 14 if abi >= 2 else 13)) - 1
        read = (1 << 0) | (1 << 2) | (1 << 3)
        write = (1<<1)|(1<<4)|(1<<5)|(1<<7)|(1<<8)|(1<<12)|(1<<13)|(1<<14)
        fd = syscall(444, ctypes.byref(Ruleset(handled)), 8, 0)
        try:
            for path, access in [(p, read) for p in readable] + [(p, read | write) for p in writable]:
                path = Path(path)
                if not path.exists():
                    continue
                if not path.is_dir():
                    access &= (1 << 0) | (1 << 1) | (1 << 2) | (1 << 14)
                target = os.open(path, os.O_PATH | os.O_CLOEXEC)
                try:
                    rule = PathRule(access & handled, target)
                    syscall(445, fd, 1, ctypes.byref(rule), 0)
                finally:
                    os.close(target)
            if libc.prctl(38, 1, 0, 0, 0):
                raise OSError(ctypes.get_errno(), 'no_new_privs')
            syscall(446, fd, 0)
        finally:
            os.close(fd)
    machine = platform.machine()
    # Native and compat syscall ABIs cannot bypass the network/process barriers.
    if machine == 'x86_64':
        arch = 0xc000003e
        denied = [41,42,43,44,45,46,47,48,49,50,53,56,57,58,59,62,101,155,161,165,166,167,168,169,170,171,172,173,174,175,176,200,234,246,248,249,250,272,288,297,298,299,304,308,310,311,313,321,322,323]
        # clone is needed by numerical-library threads, but fork/exec are not.
        denied.remove(56)
    elif machine in ('aarch64', 'arm64'):
        arch = 0xc00000b7
        denied = [39,40,41,51,97,104,105,106,107,117,129,130,131,142,198,199,200,201,202,203,206,207,211,212,221,224,241,242,243,268,270,271,280,281,282]
    else:
        raise RuntimeError('Unsupported seccomp architecture: ' + machine)
    denied += [425,426,427,428,429,430,431,432,433,434,435,438,440,442,448]
    class Filter(ctypes.Structure):
        _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte), ('jf', ctypes.c_ubyte), ('k', ctypes.c_uint32)]
    class Program(ctypes.Structure):
        _fields_ = [('length', ctypes.c_ushort), ('filters', ctypes.POINTER(Filter))]
    instructions = [Filter(0x20,0,0,4), Filter(0x15,1,0,arch), Filter(0x06,0,0,0x80000000), Filter(0x20,0,0,0)]
    if machine == 'x86_64':
        instructions += [Filter(0x35,0,1,0x40000000), Filter(0x06,0,0,0x80000000)]
    if branch:
        denied += [112,109] if machine=='x86_64' else [157,154]
        clone_number=56 if machine=='x86_64' else 220
        instructions += [Filter(0x15,0,4,clone_number),Filter(0x20,0,0,16),Filter(0x45,1,0,0x10000),Filter(0x06,0,0,0x50000|errno.EPERM),Filter(0x20,0,0,0)]
    for number in sorted(set(denied)):
        instructions += [Filter(0x15,0,1,number), Filter(0x06,0,0,0x50000 | (errno.ENOSYS if number==435 else errno.EPERM))]
    instructions += [Filter(0x06,0,0,0x7fff0000)]
    filters = (Filter * len(instructions))(*instructions)
    if libc.prctl(22, 2, ctypes.byref(Program(len(instructions), filters)), 0, 0):
        raise OSError(ctypes.get_errno(), 'seccomp filter')
    return abi



def process_status():
    fields={'Seccomp','NoNewPrivs','CapEff','CapPrm','CapInh'}
    return {key:value.strip() for line in Path('/proc/self/status').read_text().splitlines() if ':' in line for key,value in [line.split(':',1)] if key in fields}


def provider_limits():
    raw={}
    for name in ('/sys/fs/cgroup/cpu.max','/sys/fs/cgroup/memory.max',
                 '/sys/fs/cgroup/cpu/cpu.cfs_quota_us','/sys/fs/cgroup/cpu/cpu.cfs_period_us',
                 '/sys/fs/cgroup/memory/memory.limit_in_bytes'):
        try:raw[name]=Path(name).read_text().strip()
        except OSError:pass
    cpu=memory=None
    if '/sys/fs/cgroup/cpu.max' in raw:
        quota,period=raw['/sys/fs/cgroup/cpu.max'].split()
        if quota!='max':cpu=int(quota)/int(period)
    elif '/sys/fs/cgroup/cpu/cpu.cfs_quota_us' in raw and '/sys/fs/cgroup/cpu/cpu.cfs_period_us' in raw:
        quota=int(raw['/sys/fs/cgroup/cpu/cpu.cfs_quota_us'])
        if quota>0:cpu=quota/int(raw['/sys/fs/cgroup/cpu/cpu.cfs_period_us'])
    for name in ('/sys/fs/cgroup/memory.max','/sys/fs/cgroup/memory/memory.limit_in_bytes'):
        if name in raw and raw[name]!='max':
            amount=int(raw[name])
            if amount<2**60:memory=amount
            break
    return {'cpu_quota_cores':cpu,'memory_cgroup_limit_bytes':memory,'cpu_affinity_count':len(os.sched_getaffinity(0)),
            'vm_mem_total_bytes':os.sysconf('SC_PHYS_PAGES')*os.sysconf('SC_PAGE_SIZE'),
            'worker_address_space_limit_mb':int(os.getenv('WABBER_WORKER_MEMORY_MB','3072')),
            'process_status':process_status(),'raw_cgroup':raw,'note':'VM memory and affinity are observations, not substitutes for an unavailable provider resource quota'}


def clean_environment(root):
    env = {k: os.environ[k] for k in ('PATH','PYTHONPATH','PYTHONUSERBASE','LD_LIBRARY_PATH','LANG') if k in os.environ}
    env.update(HOME=str(root), TMPDIR=str(root/'tmp'), MPLCONFIGDIR=str(root/'cache'),
               OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1', NUMEXPR_NUM_THREADS='1',
               PYTHONHASHSEED='0', PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1')
    return env


def serve(config_path):
    config = json.loads(Path(config_path).read_text())
    root = Path(config['root']); inputs = Path(config['inputs']); workspace = Path(config['workspace'])
    sys.path.insert(0, str(Path(__file__).parent))
    readable = [Path(p).resolve() for p in sys.path if p and Path(p).exists()]
    readable += [Path(p) for p in ('/usr','/lib','/lib64','/var/lang','/var/runtime','/etc/fonts','/etc/matplotlibrc','/dev/urandom','/dev/random')]
    # Discover native math libraries before filesystem confinement; generated code cannot read /proc.
    from sklearn import cluster,ensemble
    from sklearn.utils.fixes import _get_threadpool_controller
    _get_threadpool_controller()
    # The worker never inherits SDK sockets or credentials, and cannot open /proc.
    frozen=config['confinement']=='frozen_trace'
    abi = restrict_process(readable + [inputs, workspace], [root, Path('/dev/null')],landlock=not frozen,platform_seccomp=config['process_isolation']=='lambda_microvm')
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    resource.setrlimit(resource.RLIMIT_NPROC,(128,128))
    resource.setrlimit(resource.RLIMIT_FSIZE, (536870912,536870912))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256,256))
    memory = int(config['memory_mb']) * 1024**2
    resource.setrlimit(resource.RLIMIT_AS, (memory,memory))
    from sandbox_worker import run
    shared = {}; pending = None; children = {}
    wake_read=config['wake_read'];wake_write=config['wake_write']
    os.set_blocking(wake_read,False);os.set_blocking(wake_write,False)
    def notify(*_):
        try:os.write(wake_write,b'1')
        except BlockingIOError:pass
    signal.signal(signal.SIGCHLD,notify)
    ready=root/'ready.tmp';ready.write_text(json.dumps({'pid':os.getpid(),'landlock_abi':abi}))
    ready.replace(root/'ready')
    while True:
        for child in list(children):
            pid, _ = os.waitpid(child, os.WNOHANG)
            if pid:
                job_root=children.pop(child)
                try:unfinished=not (job_root/'done').stat().st_size
                except FileNotFoundError:unfinished=False
                if unfinished:
                    (job_root/'result.json').write_text(json.dumps({'status':'error','error_type':'WorkerExit','error':f'Worker exited without a result: wait status {_}'}))
                    (job_root/'done').write_text('1')
        for queued in sorted((root/'queue').glob('*.json')):
            job = json.loads(queued.read_text()); queued.unlink()
            out = Path(job['out']); request = job['request']; parent = Path(job['parent'])
            args = (request, parent, out, inputs, Path(job['branches']))
            mode = request['mode']
            if mode in ('commit','abort'):
                valid = pending is not None and pending[0] == request['token']
                if valid:
                    if mode == 'commit': shared = pending[1]
                    pending = None
                result = {'status':'ok'} if valid else {'status':'error','error_type':'ValueError','error':'Stale resident merge token'}
                (out.parent/'result.json').write_text(json.dumps(result)); (out.parent/'done').write_text('1')
                continue
            if config['resident'] and mode == 'merge':
                candidate = dict(shared)
                result = run(*args, shared=candidate, resident=True)
                if result['status'] == 'ok':
                    pending = (queued.stem, candidate); result['merge_token'] = queued.stem
                (out.parent/'result.json').write_text(json.dumps(result)); (out.parent/'done').write_text('1')
                continue
            # Fork happens in the credential-free single-threaded resident worker.
            # A child receives a read-only snapshot of the canonical Python heap.
            pid = os.fork()
            if pid == 0:
                try:
                    signal.signal(signal.SIGCHLD,signal.SIG_DFL)
                    os.close(wake_read);os.close(wake_write)
                    os.setsid()
                    resource.setrlimit(resource.RLIMIT_CPU, (int(config['timeout_s']),int(config['timeout_s'])+1))
                    restrict_process(readable + [inputs,parent,Path(job['branches'])], [out,out.parent/'result.json',out.parent/'done',root/'cache',root/'tmp',Path('/dev/null')],branch=True,landlock=not frozen,platform_seccomp=config['process_isolation']=='lambda_microvm')
                    if frozen:
                        from frozen_policy import file_guard
                        file_guard(readable+[inputs,parent,Path(job['branches'])],[out,out.parent/'result.json',out.parent/'done',root/'cache',root/'tmp',Path('/dev/null')])
                    run(*args, shared=shared if config['resident'] else None, resident=config['resident'])
                except BaseException as exc:
                    (out.parent/'result.json').write_text(json.dumps({'status':'error','error_type':type(exc).__name__,'error':str(exc)}))
                finally:
                    (out.parent/'done').write_text('1'); os._exit(0)
            children[pid] = out.parent
            (out.parent/'started.json').write_text(json.dumps({'pid':pid}))
        select.select([wake_read],[],[])
        try:os.read(wake_read,65536)
        except BlockingIOError:pass


class NativeKernel:
    def __init__(self, workspace, inputs, *, resident=True):
        self.workspace = Path(workspace).resolve(); self.inputs = Path(inputs).resolve()
        self.provider_limits=provider_limits()
        self.confinement=os.getenv('WABBER_NATIVE_CONFINEMENT','landlock')
        if self.confinement not in {'landlock','frozen_trace'}:raise ValueError('Unknown native confinement policy')
        self.resident = resident; self.name = 'native-' + uuid.uuid4().hex; self.closed = False
        self.clock_domain=hashlib.sha256(self.name.encode()).hexdigest()
        declared=os.getenv('WABBER_PROCESS_ISOLATION','nested_seccomp')
        if declared not in {'nested_seccomp','lambda_microvm'}:raise ValueError('Unknown process isolation policy')
        self.process_isolation='lambda_microvm' if declared=='lambda_microvm' and os.getenv('PROVIDER')=='aws_lambda' and os.getenv('AWS_LAMBDA_FUNCTION_NAME') else 'nested_seccomp'
        if self.process_isolation=='lambda_microvm' and (self.confinement!='frozen_trace' or os.getenv('ATSUITE_ALLOW_STATE_BOOTSTRAP')!='false'):
            raise ValueError('Lambda platform policy only supports image-approved, bootstrap-disabled frozen replay')
        self.directory = Path(tempfile.mkdtemp(prefix='wabber-native-'))
        for name in ('queue','tmp','cache'):(self.directory/name).mkdir()
        wake_read,self.wake_write=os.pipe()
        config = {'wake_read':wake_read,'wake_write':self.wake_write,'root':str(self.directory),'workspace':str(self.workspace),'inputs':str(self.inputs),
                  'resident':resident,'process_isolation':self.process_isolation,'confinement':self.confinement,'memory_mb':int(os.getenv('WABBER_WORKER_MEMORY_MB','3072')),
                  'timeout_s':int(os.getenv('WABBER_CODE_TIMEOUT','180'))}
        path = self.directory/'config.json'; path.write_text(json.dumps(config))
        self.errors = (self.directory/'stderr.txt').open('w')
        started = time.perf_counter()
        try:
            self.process = subprocess.Popen([sys.executable,str(Path(__file__).resolve()),str(path)],
                env=clean_environment(self.directory),cwd=self.directory,close_fds=True,pass_fds=(wake_read,self.wake_write),start_new_session=True,
                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=self.errors)
        except BaseException:
            os.close(self.wake_write);self.errors.close();shutil.rmtree(self.directory,ignore_errors=True);raise
        finally:os.close(wake_read)
        atexit.register(self.close)
        try:
            while not (self.directory/'ready').exists():
                self.check()
                if time.perf_counter()-started > 60:raise TimeoutError('Native sandbox startup timeout')
                time.sleep(.02)
            ready = json.loads((self.directory/'ready').read_text()); self.pid = ready['pid']; self.landlock_abi = ready['landlock_abi']
            self.start_s = time.perf_counter()-started
        except BaseException:
            self.close(); raise

    def check(self):
        if self.process.poll() is not None:
            error=(self.directory/'stderr.txt').read_text(errors='replace')[-8000:]
            self.close()
            raise RuntimeError('Native sandbox failed closed: '+error)

    def run(self, request, parent, inputs=None, branches=None):
        try:from .guards import safe_files
        except ImportError:from guards import safe_files
        if self.closed:raise RuntimeError('Native kernel closed')
        self.check(); token = uuid.uuid4().hex; job_root = self.directory/token; job_root.mkdir(); out = job_root/'out'; out.mkdir()
        for name in ('result.json','done'):(job_root/name).touch()
        aliases = {}
        for directory in (self.inputs,Path(parent)/'artifacts'):
            for p in directory.rglob('*'):
                if p.is_file():
                    name = p.relative_to(directory).as_posix()
                    if name not in request.get('outputs',[]):aliases[name] = str(p.resolve())
        aliases.update(inputs_dir=str(self.inputs),output_dir=str(out))
        for name,target in aliases.items():
            p = out/name; p.parent.mkdir(parents=True,exist_ok=True); p.symlink_to(target)
        if branches:shutil.copytree(branches,job_root/'branches')
        job = {'request':request,'out':str(out),'parent':str(Path(parent).resolve()),'branches':str(job_root/'branches')}
        temp = job_root/'command.json';temp.write_text(json.dumps(job));temp.replace(self.directory/'queue'/(token+'.json'))
        started = time.perf_counter()
        try:
            try:os.write(self.wake_write,b'1')
            except BlockingIOError:pass
            while not (job_root/'done').stat().st_size:
                self.check();safe_files(out,aliases)
                if time.perf_counter()-started > float(os.getenv('WABBER_CODE_TIMEOUT','180')):
                    # The parent can kill sandbox children; untrusted children cannot signal others.
                    if (job_root/'started.json').exists():
                        os.killpg(json.loads((job_root/'started.json').read_text())['pid'],signal.SIGKILL)
                        (job_root/'done').write_text('killed')
                    else:self.close()
                    raise TimeoutError('Generated code exceeded sandbox time limit')
                time.sleep(.02)
            result = json.loads((job_root/'result.json').read_text())
            result.update(sandbox_wall_s=time.perf_counter()-started,container_name=self.name,resident_kernel_pid=self.pid if self.resident else None,worker_server_pid=self.pid,worker_server_lifecycle='session-resident' if self.resident else 'instance-reused-stateless',
                          sandbox_backend='native-'+self.confinement+('-platform-microvm' if self.process_isolation=='lambda_microvm' else '-seccomp'),process_isolation=self.process_isolation,landlock_abi=self.landlock_abi,kernel_reused=self.resident,clock_domain=self.clock_domain,provider_limits=self.provider_limits)
            for name,target in aliases.items():
                p = out/name
                if not p.is_symlink() or os.readlink(p)!=target:raise ValueError('Sandbox input alias modified')
                p.unlink()
            safe_files(out)
            if result['status']!='ok' or request['mode'] in ('commit','abort'):return result,None
            fd,name=tempfile.mkstemp(prefix='wabber-native-bundle-',suffix='.tar');os.close(fd)
            with tarfile.open(name,'w') as archive:
                for p in safe_files(out):
                    relative=p.relative_to(out).as_posix()
                    if relative in {'state.pkl','stdout.txt',*request.get('outputs',[])}:archive.add(p,arcname=relative,recursive=False)
            return result,Path(name)
        finally:
            if not self.closed and not (job_root/'done').stat().st_size:self.close()
            shutil.rmtree(job_root,ignore_errors=True)

    def close(self):
        if self.closed:return
        self.closed=True
        for started in self.directory.glob('*/started.json'):
            try:os.killpg(json.loads(started.read_text())['pid'],signal.SIGKILL)
            except ProcessLookupError:pass
        if self.process.poll() is None:
            os.killpg(self.process.pid,signal.SIGKILL);self.process.wait(timeout=10)
        os.close(self.wake_write)
        self.errors.close();shutil.rmtree(self.directory,ignore_errors=True)


if __name__=='__main__':serve(sys.argv[1])
