"""Explicit Docker security checks; fixtures are not benchmark workload or timings."""
import json,os,pickle,tempfile,time,uuid
from pathlib import Path
import subprocess
from benchmarks.DataSciBench.nodes.AgentTools.implementation import sandbox,store
from atsuite_sdk.state import get_state_runtime


def run():
    image=os.environ['WABBER_SANDBOX_IMAGE'];results=[]
    with tempfile.TemporaryDirectory(prefix='wabber-security-') as directory:
        root=Path(directory);(root/'state.pkl').write_bytes(pickle.dumps({}));(root/'artifacts').mkdir()
        code="""import os,socket
from pathlib import Path
assert not any(os.getenv(k) for k in ('DEEPSEEK_API_KEY','OPENAI_API_KEY','AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','HF_TOKEN'))
assert not Path('/var/run/docker.sock').exists()
assert not any(line.split()[4]=='/repo' for line in Path('/proc/self/mountinfo').read_text().splitlines())
assert not os.access('/root/.aws/credentials',os.R_OK)
blocked=False
try:socket.create_connection(('1.1.1.1',443),timeout=.2)
except OSError:blocked=True
assert blocked,'Network escaped sandbox'
try:Path('/snapshot/forbidden').write_text('bad')
except OSError:pass
else:raise AssertionError('Read-only snapshot was writable')
state_out={'network_blocked':True,'credential_free':True,'snapshot_readonly':True}
"""
        result,bundle=sandbox({'mode':'analyze','code':code,'outputs':[],'seed':0},root)
        assert result['status']=='ok',result
        bundle.unlink();results.append({'check':'network, credentials, host mounts, read-only input','status':'passed'})
        os.environ['WABBER_CODE_TIMEOUT']='2.5';start=time.perf_counter()
        try:sandbox({'mode':'analyze','code':'while True: pass','outputs':[],'seed':0},root)
        except TimeoutError:results.append({'check':'timeout kills container','status':'passed','actual_wall_s':time.perf_counter()-start})
        else:raise AssertionError('Timeout not enforced')
        os.environ['WABBER_CODE_TIMEOUT']='180'
        try:
            result,bundle=sandbox({'mode':'analyze','code':"import os\nos.symlink('/etc/passwd',output_dir/'link')",'outputs':['link'],'seed':0},root)
        except ValueError:results.append({'check':'reject symbolic link outputs','status':'passed'})
        else:
            if bundle:bundle.unlink()
            raise AssertionError('Symbolic link was accepted')
    key='security-test/'+uuid.uuid4().hex+'.tar'
    with tempfile.TemporaryDirectory() as directory:
        source=Path(directory)/'source';source.write_bytes(b'original');store(key,source=source)
        source.write_bytes(b'incompatible')
        try:store(key,source=source)
        except FileExistsError:pass
        else:raise AssertionError('Immutable branch was overwritten')
        target=Path(directory)/'read';store(key,destination=target);assert target.read_bytes()==b'original'
        (get_state_runtime().root/'branch_artifacts'/key).unlink()
        results.append({'check':'immutable branch objects','status':'passed'})
    out=Path('results/phase2')/('sandbox-security-'+uuid.uuid4().hex+'.json')
    out.write_text(json.dumps({'image_id':image,'fixture_scope':'adversarial tests only; not benchmark data','checks':results},indent=2)+'\n')
    print(out)


if __name__=='__main__':run()
