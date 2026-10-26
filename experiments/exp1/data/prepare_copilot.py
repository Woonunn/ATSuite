"""Prepare pinned public trace assets before any timed workload runs."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path,PurePosixPath
import shutil
import tarfile
import tempfile
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[3]
LOCK=Path(__file__).with_name('copilot_source.lock.json')
DEFAULT=ROOT/'results/phase3/copilot'


def sha(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def download(asset,target):
    expected=asset['digest'].removeprefix('sha256:')
    if target.exists():
        if target.stat().st_size!=asset['size'] or sha(target)!=expected:raise ValueError('Existing archive checksum mismatch: '+target.name)
        return
    part=target.with_suffix(target.suffix+'.part')
    for attempt in range(3):
        try:
            offset=part.stat().st_size if part.exists() else 0
            if offset>asset['size']:part.unlink();offset=0
            headers={'User-Agent':'Wabber-Copilot-preparation/1'}
            if offset:headers['Range']=f'bytes={offset}-'
            request=urllib.request.Request(asset['browser_download_url'],headers=headers)
            with urllib.request.urlopen(request,timeout=90) as incoming:
                append=offset>0 and incoming.status==206
                if append and not incoming.headers.get('Content-Range','').startswith(f'bytes {offset}-'):raise ValueError('Wrong download range')
                with part.open('ab' if append else 'wb') as outgoing:shutil.copyfileobj(incoming,outgoing,1024*1024)
            if part.stat().st_size!=asset['size'] or sha(part)!=expected:
                part.unlink();raise ValueError('Downloaded asset checksum/size mismatch')
            part.replace(target);return
        except Exception:
            if attempt==2:raise
            time.sleep(2**attempt)


def extract(archive,root,date):
    target=root/f'date={date}';marker=target/'_verified.json'
    if marker.exists():
        previous=json.loads(marker.read_text())
        if previous['archive_sha256']!=sha(archive):raise ValueError('Changed source archive')
        for entry in previous['files']:
            if sha(target/entry['name'])!=entry['sha256']:raise ValueError('Changed extracted shard')
        return previous
    if target.exists():raise ValueError('Unverified partition already exists: '+str(target))
    with tempfile.TemporaryDirectory(prefix='.extract-',dir=root) as directory:
        staging=Path(directory);partition=staging/f'date={date}';partition.mkdir()
        with tarfile.open(archive,'r|gz') as incoming:
            for member in incoming:
                name=member.name
                while name.startswith('./'):name=name[2:]
                path=PurePosixPath(name)
                if member.isdir():continue
                if not member.isfile() or path.is_absolute() or '..' in path.parts or '\\' in name:raise ValueError('Unsafe archive member')
                if len(path.parts)!=2 or path.parts[0]!=f'date={date}' or not (path.name=='manifest.json' or path.name.endswith('.jsonl.gz')):
                    raise ValueError('Unexpected archive member: '+name)
                destination=staging/path
                if destination.exists():raise ValueError('Duplicate archive member')
                with incoming.extractfile(member) as source,destination.open('wb') as output:shutil.copyfileobj(source,output,1024*1024)
        if not (partition/'manifest.json').is_file() or not list(partition.glob('*.jsonl.gz')):raise ValueError('Incomplete trace partition')
        files=[{'name':p.name,'bytes':p.stat().st_size,'sha256':sha(p)} for p in sorted(partition.iterdir())]
        record={'date':date,'archive_sha256':sha(archive),'files':files}
        (partition/'_verified.json').write_text(json.dumps(record,indent=2)+'\n');partition.replace(target)
        return record


def prepare(output=DEFAULT,workers=3):
    output=Path(output);archives=output/'archives';raw=output/'raw';archives.mkdir(parents=True,exist_ok=True);raw.mkdir(exist_ok=True)
    lock=json.loads(LOCK.read_text())
    def one(asset):
        date=asset['name'].removeprefix('date.').removesuffix('.tar.gz')
        archive=archives/asset['name'];download(asset,archive);record=extract(archive,raw,date)
        print(date,'verified',len(record['files'])-1,'shards',flush=True);return record
    with ThreadPoolExecutor(max_workers=workers) as pool:records=list(pool.map(one,lock['assets']))
    result={'release':lock['release_url'],'lock_sha256':sha(LOCK),'partitions':records,'status':'verified'}
    (output/'preparation.json').write_text(json.dumps(result,indent=2)+'\n');return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,default=DEFAULT);p.add_argument('--workers',type=int,default=3)
    a=p.parse_args();prepare(a.output,a.workers)
