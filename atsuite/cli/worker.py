import argparse
import json
from pathlib import Path
from atsuite.execution.postgres_store import open_store
from atsuite.recommender import service

def main(argv=None):
    from atsuite.config import load_local_env
    load_local_env()
    parser=argparse.ArgumentParser(description='Run approved experiments with built-in ATSuite handlers')
    parser.add_argument('--store',required=True);parser.add_argument('--experiment')
    parser.add_argument('--poll',action='store_true');parser.add_argument('--interval',type=float,default=2)
    parser.add_argument('--handlers',help='local administrator extension module:factory')
    parser.add_argument('--allow-custom-handlers',action='store_true')
    parser.add_argument('--reconcile-ttl',action='store_true')
    args=parser.parse_args(argv)
    if not args.experiment and not args.poll and not args.reconcile_ttl:parser.error('--experiment, --poll or --reconcile-ttl required')
    if args.handlers and not args.allow_custom_handlers:parser.error('custom handler imports require local --allow-custom-handlers')
    if args.interval<=0:parser.error('interval must be positive')
    store=open_store(args.store);exit_code=0
    try:
        if args.reconcile_ttl:
            from atsuite.execution.cleanup import reconcile_expired
            print(json.dumps(reconcile_expired(store)));return 0
        while True:
            for experiment in ([args.experiment] if args.experiment else store.queued()):
                if args.handlers:
                    from atsuite.execution.worker import load_handlers
                    from atsuite.execution.jobs import ExperimentJob
                    record=store.get(experiment)
                    service.validate_approval(store,record['plan'],record['approval'],tenant=record['tenant'],project=record['project'])
                    result=ExperimentJob(store,experiment,load_handlers(args.handlers,store,experiment)).run()
                else:result=service.run(store,experiment)
                print(json.dumps(result,ensure_ascii=False,default=str),flush=True)
                if result['state'] in {'FAILED','CANCELLED'}:exit_code=1
            if not args.poll:break
            import time;time.sleep(min(args.interval,30))
    except KeyboardInterrupt:return exit_code
    finally:store.close()
    return exit_code
if __name__=='__main__':raise SystemExit(main())
