"""Adversarial lifecycle/commit checks in one actual resident Docker environment."""
import json,os,tempfile,uuid
from pathlib import Path
from benchmarks.DataSciBench.nodes.AgentTools.resident_kernel import ResidentKernel
from benchmarks.DataSciBench.nodes.AgentTools.implementation import unpack
ROOT=Path(__file__).resolve().parents[3]


def main():
    image=os.environ['WABBER_SANDBOX_IMAGE'];checks=[]
    with tempfile.TemporaryDirectory(prefix='wabber-resident-check-') as directory:
        root=Path(directory);parent=root/'v0';(parent/'artifacts').mkdir(parents=True)
        kernel=ResidentKernel(root,ROOT/'benchmarks/DataSciBench/data/human_15',image)
        try:
            def analyze(code):
                result,bundle=kernel.run({'mode':'analyze','code':code,'outputs':[],'seed':0},parent)
                return result,bundle
            produced,bundle=analyze("import numpy as np\nstate_out={'data':np.arange(10000)}")
            assert produced['status']=='ok',produced
            branches=root/'branches';(branches/'producer').mkdir(parents=True);unpack(bundle,branches/'producer');bundle.unlink()
            merged,bundle=kernel.run({'mode':'merge','steps':['producer']},parent,branches=branches);bundle.unlink()
            before,bundle=analyze("assert 'producer' not in shared\nstate_out={}");assert before['status']=='ok',before;bundle.unlink()
            kernel.run({'mode':'abort','token':merged['merge_token']},parent)
            result,bundle=analyze("assert 'producer' not in shared\nstate_out={}");assert result['status']=='ok';bundle.unlink()
            checks.append('Unpublished merge is invisible; abort preserves the old canonical heap')
            merged,bundle=kernel.run({'mode':'merge','steps':['producer']},parent,branches=branches);bundle.unlink()
            committed,_=kernel.run({'mode':'commit','token':merged['merge_token']},parent);assert committed['status']=='ok'
            stale,_=kernel.run({'mode':'commit','token':merged['merge_token']},parent);assert stale['status']=='error'
            result,bundle=analyze("assert shared['producer']['data'].sum()==49995000\nstate_out={}");assert result['status']=='ok';bundle.unlink()
            checks.append('Commit publishes native arrays; stale commit tokens fail')
            code="""import os,socket,signal
from pathlib import Path
assert not any(os.getenv(k) for k in ('DEEPSEEK_API_KEY','AWS_ACCESS_KEY_ID','HF_TOKEN'))
try:socket.create_connection(('1.1.1.1',443),timeout=.2)
except OSError:pass
else:raise AssertionError('External network available')
for p in ('/snapshot/corrupt','/session/queue/injected.json'):
 try:Path(p).write_text('bad')
 except OSError:pass
 else:raise AssertionError('Protected path writable: '+p)
try:os.kill(os.getppid(),signal.SIGUSR1)
except PermissionError:pass
else:raise AssertionError('Branch can signal resident parent')
assert shared['producer']['data'].sum()==49995000
state_out={}
"""
            result,bundle=analyze(code);assert result['status']=='ok',result;bundle.unlink()
            checks.append('No credentials/network; canonical files, command queue and parent signals protected')
            os.environ['WABBER_CODE_TIMEOUT']='2.5'
            try:analyze('while True: pass')
            except TimeoutError:pass
            else:raise AssertionError('Timeout did not stop branch')
            finally:os.environ['WABBER_CODE_TIMEOUT']='180'
            result,bundle=analyze("assert shared['producer']['data'].sum()==49995000\nstate_out={}");assert result['status']=='ok',result;bundle.unlink()
            checks.append('Timeout kills only the branch; committed resident state survives')
        finally:kernel.close()
    target=ROOT/'results/phase2'/('resident-security-'+uuid.uuid4().hex+'.json')
    target.write_text(json.dumps({'scope':'actual Docker isolation; adversarial fixtures, not benchmark computation','image':image,'checks':checks},indent=2)+'\n');print(target)


if __name__=='__main__':main()
