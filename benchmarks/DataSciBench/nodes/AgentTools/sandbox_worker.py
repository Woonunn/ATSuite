"""Runs only inside a credential-free Docker sandbox, including unpickling state."""
import hashlib,json,os,pickle,sys,time,traceback
from pathlib import Path
from types import ModuleType
from joblib.externals import cloudpickle
from kernel import execute_code


def canonical(value,depth=0):
    import numpy as np
    import pandas as pd
    if type(value).__module__.startswith('torch'):
        import torch
        if isinstance(value,torch.Tensor):return canonical(value.detach().cpu().numpy(),depth+1)
        if isinstance(value,torch.nn.Module):return {'torch_module':type(value).__name__,'weights':canonical(dict(value.state_dict()),depth+1)}
    if depth>20:raise ValueError('State cannot be compared deterministically')
    if value is None or isinstance(value,(str,bool,int)):return value
    if isinstance(value,(float,np.floating)):
        return format(float(value),'.8g')
    if isinstance(value,np.integer):return int(value)
    if isinstance(value,(np.ndarray,pd.DataFrame,pd.Series)):
        if isinstance(value,pd.DataFrame):
            return {'frame':hashlib.sha256(value.to_json(orient='split',double_precision=8,date_format='iso').encode()).hexdigest()}
        if isinstance(value,pd.Series):return {'series':value.to_json(double_precision=8,date_format='iso')}
        if value.dtype.fields:return {'dtype':str(value.dtype),'fields':{k:canonical(value[k],depth+1) for k in value.dtype.names}}
        if np.issubdtype(value.dtype,np.number):
            return {'shape':list(value.shape),'dtype':str(value.dtype),'sha256':hashlib.sha256(np.round(value.astype(float),8).tobytes()).hexdigest()}
        return {'shape':list(value.shape),'values':value.tolist()}
    if isinstance(value,dict):return {str(k):canonical(v,depth+1) for k,v in sorted(value.items(),key=lambda x:str(x[0]))}
    if isinstance(value,(list,tuple)):return [canonical(v,depth+1) for v in value]
    if isinstance(value,(set,frozenset)):return sorted(canonical(v,depth+1) for v in value)
    if hasattr(value,'get_params'):
        return {'class':type(value).__module__+'.'+type(value).__name__,
                'parameters':canonical(value.get_params(deep=False),depth+1),
                'fitted':canonical({k:v for k,v in vars(value).items() if k.endswith('_')},depth+1)}
    if hasattr(value,'__getstate__'):
        state=value.__getstate__()
        if state is not None:return {'class':type(value).__name__,'state':canonical(state,depth+1)}
    raise ValueError('Unsupported state comparison type: '+type(value).__name__)


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def artifact(path,out):
    import pandas as pd
    suffix=path.suffix.lower();raw=path.read_bytes()
    signature=hashlib.sha256(raw).hexdigest();method='sha256'
    if suffix=='.csv':signature=digest(canonical(pd.read_csv(path)));method='csv-round-8-decimal-places'
    elif suffix=='.json':signature=digest(json.loads(raw));method='json'
    elif suffix=='.png':
        from PIL import Image
        with Image.open(path) as im:
            im=im.convert('RGBA');signature=digest({'shape':im.size,'pixels':hashlib.sha256(im.tobytes()).hexdigest()})
        method='decoded-rgba'
    elif suffix in ('.pt','.pth'):
        import torch
        signature=digest(canonical(torch.load(path,map_location='cpu',weights_only=True)));method='torch-weights-only-canonical'
    elif suffix=='.pdf':
        from pypdf import PdfReader
        reader=PdfReader(path);signature=digest([(p.extract_text(),[hashlib.sha256(i.data).hexdigest() for i in p.images]) for p in reader.pages]);method='pdf-text-and-images'
    return {'name':path.relative_to(out).as_posix(),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),
            'signature':signature,'comparison':method}


def run(request,parent,out,inputs=Path('/inputs'),branches=Path('/branches'),shared=None,resident=False):
    out.mkdir(exist_ok=True)
    started=time.perf_counter();result={'status':'ok'}
    try:
        state_path=parent/'state.pkl'
        restored=shared is None
        restore_start=time.perf_counter()
        if restored:shared=cloudpickle.loads(state_path.read_bytes()) if state_path.exists() else {}
        result['state_lifecycle']={'mode':'resident' if resident else 'restore-persist',
            'canonical_restore_performed':restored,'canonical_restore_s':time.perf_counter()-restore_start if restored else None,
            'canonical_restore_bytes':state_path.stat().st_size if restored and state_path.exists() else None,
            'canonical_save_performed':False,'canonical_save_s':None,'canonical_save_bytes':None}
        if request['mode']=='merge':
            for step in request['steps']:
                shared[step]=cloudpickle.loads((branches/step/'state.pkl').read_bytes())
            if not resident:
                save_start=time.perf_counter()
                (out/'state.pkl').write_bytes(cloudpickle.dumps(shared,protocol=5))
                result['state_lifecycle'].update(canonical_save_performed=True,canonical_save_s=time.perf_counter()-save_start,canonical_save_bytes=(out/'state.pkl').stat().st_size)
            result.update(state_signature=digest(canonical(shared)))
        elif request['mode']=='analyze':
            import matplotlib
            matplotlib.use('Agg')
            from matplotlib.figure import Figure
            recorded={};original_save=Figure.savefig
            def save(figure,filename,*args,**kwargs):
                result=original_save(figure,filename,*args,**kwargs)
                path=Path(filename).resolve()
                if path.is_relative_to(out/'output_dir'):path=out/path.relative_to(out/'output_dir')
                name=path.relative_to(out).as_posix()
                bars=[];lines=[];scatter=[]
                for ax in figure.axes:
                    for patch in ax.patches:
                        if hasattr(patch,'get_height'):
                            bars.append({'height':float(patch.get_height()),'x':float(patch.get_x()),'width':float(patch.get_width()),'face':list(patch.get_facecolor()),'edge':list(patch.get_edgecolor())})
                    for line in ax.lines:
                        y=line.get_ydata()
                        lines.append({'label':line.get_label(),'style':line.get_linestyle(),'y':[float(x) if __import__('math').isfinite(float(x)) else None for x in y] if len(y)<=64 else [],'count':len(y)})
                    for collection in ax.collections:
                        offsets=collection.get_offsets()
                        if len(offsets)<=64 and hasattr(collection,'get_sizes'):
                            scatter.append({'y':[float(x) for x in offsets[:,1]],'sizes':collection.get_sizes().tolist(),'alpha':collection.get_alpha(),'face':collection.get_facecolors().tolist()})
                recorded[name]={'bars':bars,'lines':lines,'scatter':scatter}
                return result
            Figure.savefig=save
            def artifact_path(name):
                p=((parent/'artifacts')/name).resolve()
                if not p.is_relative_to((parent/'artifacts')):raise ValueError('Invalid artifact path')
                return str(p)
            scope={'__name__':'__main__','__builtins__':__builtins__,'inputs_dir':inputs,
                   'output_dir':out,'shared':shared,'state_out':{},'artifact_path':artifact_path,'seed':request['seed']}
            import random,numpy as np
            random.seed(request['seed']);np.random.seed(request['seed'])
            training={};hooks=[]
            if request.get('task_id')=='csv_excel_29':
                from extended_checks import observe_training
                training,hooks=observe_training(request['seed'])
            os.chdir(out)
            execution=execute_code(request['code'],scope,out/'stdout.txt','/work/analysis.py')
            for hook in hooks:hook.remove()
            if execution['status']!='ok':
                error_type=execution['error'].strip().splitlines()[-1].split(':',1)[0]
                result.update(status='error',error_type=error_type,error=execution['error'].strip().splitlines()[-1].split(': ',1)[-1],traceback=execution['error'],stdout=execution['output'][:16000])
                return finish(result,started,out)
            exported=scope['state_out']
            if not isinstance(exported,dict):raise ValueError('state_out must be a dict')
            export_start=time.perf_counter()
            (out/'state.pkl').write_bytes(cloudpickle.dumps(exported,protocol=5))
            result['state_lifecycle'].update(branch_export_s=time.perf_counter()-export_start,branch_export_bytes=(out/'state.pkl').stat().st_size)
            files=[artifact(out/name,out) for name in request['outputs']]
            for f in files:
                if f['name'] in recorded:f['evidence']=recorded[f['name']]
                if f['name']=='model_checkpoint.pt' and request.get('task_id')=='csv_excel_29':f['evidence']={'optimizer_steps':list(training.values())}
            result.update(artifacts=files,state_signature=digest(canonical(exported)),state_names=sorted(exported),
                          stdout=(out/'stdout.txt').read_text(errors='replace')[:16000])
        elif request['mode']=='validate':
            files=[]
            for name in request['outputs']:
                p=(parent/'artifacts')/name
                if not p.is_file() or p.stat().st_size==0:raise ValueError('Missing or empty required output: '+name)
                # File parsing stays inside the Tool sandbox.
                import shutil
                target=out/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,target);files.append(artifact(target,out))
            from result_checks import validate
            checks=validate(request['task_id'],(parent/'artifacts'),inputs,request.get('artifact_evidence',{}))
            result.update(artifacts=files,state_signature=digest(canonical(shared)),checks=checks)
        else:raise ValueError('Unknown sandbox mode')
    except BaseException as exc:
        result={'status':'error','error_type':type(exc).__name__,'error':str(exc),'traceback':traceback.format_exc(),
                'stdout':(out/'stdout.txt').read_text(errors='replace')[:16000] if (out/'stdout.txt').exists() else ''}
    return finish(result,started,out)


def finish(result,started,out):
    result.update(exec_started_monotonic=started,exec_ended_monotonic=time.perf_counter(),pid=os.getpid())
    (out.parent/'result.json').write_text(json.dumps(result,allow_nan=False)+'\n')
    return result


def main():
    request=json.loads(Path('/work/request.json').read_text())
    result=run(request,Path('/snapshot'),Path('/work/out'))
    if result['status']!='ok':sys.exit(1)


if __name__=='__main__':main()
