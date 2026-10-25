"""Download authentic inputs before measurement; replay checks the recorded SHA256 lock."""
import argparse
import csv
import hashlib
import re
import json
import os
from pathlib import Path
import shutil
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / 'benchmarks/DataSciBench/data'
LOCK = ROOT / 'benchmarks/DataSciBench/tasks/datasets.lock.json'
HF_REVISION = 'f15d3e4043498febc26d7bcb7562b7e344b514f2'
SOURCES = {
 'human_12': ('https://archive.ics.uci.edu/static/public/2/adult.zip', ['adult.data','adult.test','adult.names'], 'UCI Adult; CC BY 4.0'),
 'csv_excel_34': ('https://yutatoyama.github.io/AppliedEconometrics2020/03_Stat/data_pums_2000.csv', ['data_pums_2000.csv'], 'Census 2000 5% PUMS; Yuta Toyama course distribution CC BY-NC-SA 4.0'),
 'human_142': ('https://archive.ics.uci.edu/static/public/350/default+of+credit+card+clients.zip', ['default of credit card clients.xls'], 'UCI Default of Credit Card Clients; CC BY 4.0'),
 'human_7': ('https://raw.githubusercontent.com/DiogoRibeiro7/data/1f46e1e6cba984f739095b8117f375500edf2638/datasets/online-retail-ii/raw/online_retail_II.xlsx', ['online_retail_II.xlsx'], 'UCI Online Retail II (DOI 10.24432/C5CG6D); CC BY 4.0; unmodified public mirror pinned by Git commit'),
 'human_9': ('https://archive.ics.uci.edu/static/public/45/heart+disease.zip', ['processed.cleveland.data'], 'UCI Heart Disease; CC BY 4.0'),
 'csv_excel_29': (f'https://huggingface.co/datasets/zd21/DataSciBench/resolve/{HF_REVISION}/DataSciBench-data/csv_excel_29/data.csv', ['data.csv'], 'Official DataSciBench gated release; authorized access required'),
}


def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def checked(path, record):
    if not path.is_file() or sha(path) != record['sha256']:
        raise ValueError(f'Input checksum mismatch: {path}')


def download(url, target, token=None):
    request = urllib.request.Request(url, headers={'User-Agent':'Wabber-dataset-preparation/1.0'})
    if token:
        request.add_header('Authorization', 'Bearer '+token)
    temporary = target.with_suffix(target.suffix+'.part')
    try:
        with urllib.request.urlopen(request, timeout=180) as response, temporary.open('wb') as out:
            shutil.copyfileobj(response, out)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def prepare(data=DATA, lock_path=LOCK):
    data, lock_path = Path(data), Path(lock_path)
    catalog=json.loads((ROOT/'benchmarks/DataSciBench/tasks/catalog.json').read_text())
    allowed={t['task_id'] for t in catalog['tasks']}
    lock = json.loads(lock_path.read_text()) if lock_path.exists() else {'schema_version':1, 'tasks':{}}
    lock['tasks']={k:v for k,v in lock['tasks'].items() if k in allowed}
    for task, (url, names, provenance) in SOURCES.items():
        if task not in allowed:
            continue
        directory = data/task
        directory.mkdir(parents=True, exist_ok=True)
        previous = lock['tasks'].get(task)
        if previous and previous['status']=='prepared':
            missing=False
            for entry in previous['files']:
                if (directory/entry['name']).exists(): checked(directory/entry['name'], entry)
                else: missing=True
            if not missing:
                print(task, 'verified', flush=True)
                continue
        raw = directory/('source.zip' if url.endswith('.zip') else names[0])
        try:
            if task=='csv_excel_29' and os.getenv('DATASCI_CSV29_INPUT'):
                shutil.copyfile(os.environ['DATASCI_CSV29_INPUT'], raw)
            else:
                token = os.getenv('HF_TOKEN') if task=='csv_excel_29' else None
                if task=='csv_excel_29' and not token:
                    for cache in (Path.home()/'.cache/huggingface/token',Path.home()/'.huggingface/token'):
                        if cache.exists(): token=cache.read_text().strip(); break
                download(url, raw, token)
            archive_sha = sha(raw)
            if task=='human_7' and archive_sha!='bcbe73b35f5b7babf197fb0cb983a11f5d9ff929078d4aa53d171b1f2df2e980':
                raise ValueError('Retail workbook differs from pinned original snapshot')
            if previous and previous['status']=='prepared' and archive_sha!=previous['source_sha256']:
                raise ValueError(f'Upstream bytes changed for {task}; locked inputs were not replaced')
            if raw.suffix=='.zip':
                with zipfile.ZipFile(raw) as z:
                    for name in names:
                        matches=[p for p in z.namelist() if Path(p).name==name]
                        if len(matches)!=1:
                            raise ValueError(f'Expected exactly one {name} in {task} archive: {matches}')
                        with z.open(matches[0]) as src, (directory/name).open('wb') as dst:
                            shutil.copyfileobj(src,dst)
            derivation = None
            if task=='human_9':
                columns='age sex cp trestbps chol fbs restecg thalach exang oldpeak slope ca thal target'.split()
                # Only supply the published schema; retain missing values and all original rows.
                with (directory/'processed.cleveland.data').open() as src, (directory/'heart_disease.csv').open('w') as dst:
                    w=csv.writer(dst); w.writerow(columns)
                    for row in csv.reader(src):
                        if len(row)!=14: raise ValueError('Unexpected Cleveland schema')
                        w.writerow(row)
                names=[*names,'heart_disease.csv']
                derivation='Add documented 14-column Cleveland header; retain original 303 rows and ? markers; no numerical data changes.'
            files=[{'name':n,'path':str((directory/n).relative_to(ROOT)) if directory.is_relative_to(ROOT) else str(directory/n),
                    'sha256':sha(directory/n),'bytes':(directory/n).stat().st_size} for n in names]
            if previous and previous['status']=='prepared':
                for entry in previous['files']:checked(directory/entry['name'],entry)
            lock['tasks'][task]={'status':'prepared','source_url':url,'source_sha256':archive_sha,
                                 'provenance':provenance,'derivation':derivation,'files':files}
            print(task,'prepared',[(f['name'],f['bytes']) for f in files],flush=True)
        except urllib.error.HTTPError as e:
            lock['tasks'][task]={'status':'blocked','source_url':url,'http_status':e.code,'reason':'Source access denied' if e.code in (401,403) else str(e.reason),'files':[]}
            print(task,'blocked HTTP',e.code,flush=True)
        lock_path.parent.mkdir(parents=True,exist_ok=True)
        temp=lock_path.with_suffix('.tmp');temp.write_text(json.dumps(lock,ensure_ascii=False,indent=2)+'\n');temp.replace(lock_path)
    task=next(t for t in catalog['tasks'] if t['task_id']=='human_15')
    inline=re.search(r'```\s*\n(Month,Year,Sales,Category\n.*?)\n```',task['prompt'],re.S)
    if not inline:raise ValueError('Original sales CSV block was not found')
    directory=data/'human_15';directory.mkdir(parents=True,exist_ok=True)
    path=directory/'sales.csv';content=(inline[1]+'\n').encode()
    previous=lock['tasks'].get('human_15')
    if previous and path.exists():checked(path,previous['files'][0])
    if previous and hashlib.sha256(content).hexdigest()!=previous['files'][0]['sha256']:
        raise ValueError('Embedded CSV changed from prepared input')
    path.write_bytes(content)
    lock['tasks']['human_15']={'status':'prepared','source_url':catalog['source']['repository'],
        'source_commit_sha':catalog['source']['commit_sha'],'prompt_sha256':task['prompt_sha256'],
        'derivation':'Exact fenced CSV block from original human_15 prompt, plus trailing newline',
        'files':[{'name':'sales.csv','path':str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),'sha256':sha(path),'bytes':path.stat().st_size}]}
    lock_path.write_text(json.dumps(lock,ensure_ascii=False,indent=2)+'\n')
    return lock


if __name__=='__main__':
    prepare()
