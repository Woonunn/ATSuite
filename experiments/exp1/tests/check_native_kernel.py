"""Actual Linux sandbox checks; run in Docker, never in the Harness process."""
import json
import os
from pathlib import Path
import tempfile
import time
try:
    from benchmarks.DataSciBench.nodes.AgentTools.native_kernel import NativeKernel
    from benchmarks.DataSciBench.nodes.AgentTools.implementation import unpack
except ModuleNotFoundError:
    from native_kernel import NativeKernel
    from implementation import unpack


def main():
    os.environ['AWS_SECRET_ACCESS_KEY']='fixture-do-not-inherit'
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory); parent=root/'workspace/v0';(parent/'artifacts').mkdir(parents=True)
        inputs=root/'inputs';inputs.mkdir();(inputs/'data.txt').write_text('17')
        secret=root/'secret';secret.write_text('private')
        kernel=NativeKernel(parent.parent,inputs)
        try:
            def analyze(code):
                return kernel.run({'mode':'analyze','code':code,'outputs':[],'seed':0},parent)
            result,bundle=analyze("import numpy as np\nstate_out={'value':np.arange(10000)}")
            assert result['status']=='ok',result
            branches=root/'branches';(branches/'producer').mkdir(parents=True);unpack(bundle,branches/'producer');bundle.unlink()
            merged,bundle=kernel.run({'mode':'merge','steps':['producer']},parent,branches=branches)
            assert merged['status']=='ok',merged
            bundle.unlink();committed,_=kernel.run({'mode':'commit','token':merged['merge_token']},parent);assert committed['status']=='ok'
            code=f'''import os,socket,signal
from pathlib import Path
assert os.getenv('AWS_SECRET_ACCESS_KEY') is None
assert shared['producer']['value'].sum()==49995000
assert (inputs_dir/'data.txt').read_text()=='17'
for p in ({str(secret)!r},'/proc/self/environ','/proc/1/environ'):
 try:Path(p).read_text()
 except OSError:pass
 else:raise AssertionError('Secret path readable: '+p)
try:(inputs_dir/'data.txt').write_text('bad')
except OSError:pass
else:raise AssertionError('Input writable')
try:socket.socket()
except OSError:pass
else:raise AssertionError('Network syscalls available')
try:os.kill(os.getppid(),signal.SIGUSR1)
except OSError:pass
else:raise AssertionError('Can signal resident parent')
state_out={{'observed':int(shared['producer']['value'].sum())}}
'''
            result,bundle=analyze(code);assert result['status']=='ok',result;bundle.unlink()
            assert not result['state_lifecycle']['canonical_restore_performed']
            os.environ['WABBER_CODE_TIMEOUT']='2'
            try:analyze('while True: pass')
            except TimeoutError:pass
            else:raise AssertionError('Timeout did not stop the branch')
            finally:os.environ['WABBER_CODE_TIMEOUT']='180'
            result,bundle=analyze("assert shared['producer']['value'].sum()==49995000\nstate_out={}")
            assert result['status']=='ok',result;bundle.unlink()
            def cpu_ticks():
                fields=Path(f'/proc/{kernel.pid}/stat').read_text().split()
                return int(fields[13])+int(fields[14])
            before=cpu_ticks();time.sleep(2);idle_cpu=(cpu_ticks()-before)/os.sysconf('SC_CLK_TCK')
            assert idle_cpu<.02,('Idle worker should block on its control pipe',idle_cpu)
            print(json.dumps({'idle_wait_cpu_seconds':idle_cpu,'measurement_resolution_s':1/os.sysconf('SC_CLK_TCK'),'status':'passed','backend':result['sandbox_backend'],'landlock_abi':kernel.landlock_abi,
                              'checks':['resident state continuity','commit barrier','no inherited credentials','no proc/other-file access','immutable input','no network socket','no parent signal']}))
        finally:kernel.close()


if __name__=='__main__':main()
