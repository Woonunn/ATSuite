"""Compatibility deployment entrypoint for an approved immutable experiment."""
import argparse
from pathlib import Path
from atsuite.execution.postgres_store import open_store
from atsuite.recommender import service
from atsuite.recommender.identity import file_digest

def main(argv=None):
    parser=argparse.ArgumentParser(description='Deploy through an explicitly approved experiment job')
    parser.add_argument('--config',required=True,type=Path);parser.add_argument('--provider',required=True)
    parser.add_argument('--experiment',help='approved experiment ID')
    parser.add_argument('--store',default=Path('results/recommender/jobs.sqlite'))
    args=parser.parse_args(argv)
    if not args.experiment:parser.error('cloud writes require an approved immutable plan; use atsuite-recommend plan/run then --experiment')
    store=open_store(args.store)
    try:
        record=store.get(args.experiment)
        service.validate_approval(store,record['plan'],record['approval'],tenant=record['tenant'],project=record['project'])
        if not any(c['provider']==args.provider and Path(c['generated_config']).resolve()==args.config.resolve()
                   and c['generated_config_digest']==file_digest(args.config) for c in record['plan']['candidates']):
            parser.error('config/provider is outside the approved plan')
        result=service.run(store,args.experiment)
        return 0 if result['state']=='COMPLETED' else 1
    finally:store.close()
if __name__=='__main__':raise SystemExit(main())
