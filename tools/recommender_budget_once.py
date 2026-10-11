"""Four-provider Oracle smoke test; cloud execution requires --confirm-live."""
import argparse
import contextlib
import json
import os
import time
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from atsuite.config import load_local_env
from atsuite.execution.cloud_backend import CloudBackend
from atsuite.execution.postgres_store import open_store
from atsuite.recommender import service
from atsuite.recommender.contracts import ExperimentSpec

PROVIDERS = ('aws_lambda', 'aws_agentcore', 'ali_fc', 'ali_agentrun')
LIFECYCLE = {'idleRuntimeSessionTimeout': 60, 'maxLifetime': 90}
BOUNDS = {'build': '0.02', 'deploy': '0.10', 'invoke': '0.95', 'cleanup': '0.05'}


def experiment_spec():
    return ExperimentSpec.from_dict({
        'benchmark': {'config': 'benchmarks/RecommenderOracle/config/benchmark.json'},
        'providers': list(PROVIDERS), 'regions': {p: 'us-east-1' for p in PROVIDERS},
        'objective': {'type': 'min_cost', 'base_currency': 'CNY'},
        'search': {'mode': 'grid', 'max_candidates': 4, 'topology_strategies': ['baseline'], 'max_generated_topologies': 1},
        'protocol': {'warmup_runs': 0, 'exploration_runs': 0, 'confirmation_runs': 1},
        'prediction': {'enabled': False}, 'advisor': {'enabled': False},
        'execution': {'build_workers': 1, 'deploy_workers': 1, 'measurement_workers_per_runner': 1},
        'budget': {'currency': 'CNY', 'max_estimated_spend': 5, 'max_wall_time_seconds': 3600,
                   'max_llm_tokens': 0, 'include_cleanup_reserve': True},
    })


def validate_runtime_bound(market):
    rates = {}
    for category, meters in {
        'cpu': {'USE1-Runtime:Consumption-based:vCPU', 'USE1-Runtime:Consumption-based:vCPU-v2'},
        'memory': {'USE1-Runtime:Consumption-based:Memory', 'USE1-Runtime:Consumption-based:Memory-v2'},
    }.items():
        cards = [c for c in market['rate_cards'] if c['region'] == 'us-east-1' and c['currency'] == 'USD'
                 and c['metadata'].get('attributes', {}).get('servicecode') == 'AmazonBedrockAgentCore'
                 and c['metadata']['attributes'].get('usagetype') in meters]
        if not cards:
            raise ValueError('AgentCore Runtime price coverage missing')
        rates[category] = max(Decimal(t['unit_price']) for c in cards for t in c['tiers'])
    # At most three sessions, each capped at 90 seconds; reserve 300 seconds.
    compute = (8 * rates['cpu'] + 32 * rates['memory']) * Decimal(300) / 3600 * Decimal(market['fx']['rates']['CNY'])
    if compute + Decimal('0.05') > Decimal(BOUNDS['invoke']):
        raise ValueError('Current public prices exceed the CNY 5 experiment estimate')
    return compute


class BudgetBackend(CloudBackend):
    def preflight(self, record):
        buckets = json.loads(os.environ['ATSUITE_STATE_BUCKETS'])
        from atsuite.ali.oss import AliOSS
        import alibabacloud_oss_v2 as oss
        for name in {buckets[p] for p in ('ali_fc', 'ali_agentrun')}:
            info = AliOSS(name, 'us-east-1', create_bucket=False).client.get_bucket_info(oss.GetBucketInfoRequest(bucket=name)).bucket_info
            if info.location != 'oss-us-east-1' or str(info.owner.id) != record['approval']['accounts']['ali_fc']:
                raise ValueError('OSS bucket region or owner mismatch')
        validate_runtime_bound(record['plan']['market_snapshot'])
        result = super().preflight(record)
        result['operation_estimates'] = dict(BOUNDS)
        return result

    def deploy(self, candidate, published, record, intent_callback):
        if candidate['provider'] != 'aws_agentcore':
            return super().deploy(candidate, published, record, intent_callback)
        from atsuite.aws.Agentcore import AWSAgentCore
        original = AWSAgentCore._create_or_update_runtime

        def bounded(runtime, image):
            create = runtime.agentcore_client.create_agent_runtime

            def create_with_limit(**params):
                if params['tags']['atsuite-ownership'] != record['ownership']:
                    raise ValueError('AgentCore ownership mismatch')
                return create(**params, lifecycleConfiguration=LIFECYCLE)

            with patch.object(runtime.agentcore_client, 'create_agent_runtime', side_effect=create_with_limit):
                original(runtime, image)
            observed = runtime.agentcore_client.get_agent_runtime(agentRuntimeId=runtime._runtime_id)
            if observed['lifecycleConfiguration'] != LIFECYCLE:
                raise ValueError('AgentCore session lifetime readback mismatch')

        with patch.object(AWSAgentCore, '_create_or_update_runtime', bounded):
            return super().deploy(candidate, published, record, intent_callback)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path, help='new, empty result directory')
    parser.add_argument('--market-snapshot', type=Path, help='optional fresh official price snapshot')
    parser.add_argument('--confirm-live', action='store_true')
    args = parser.parse_args(argv)
    if args.output.exists() and any(args.output.iterdir()):
        parser.error('--output must be empty; old experiments are never replayed')
    load_local_env()
    import certifi
    os.environ.setdefault('SSL_CERT_FILE', certifi.where())
    os.environ['ATSUITE_AGENTCORE_USAGE_WAIT_TIMEOUT_S'] = '60'
    os.environ['ATSUITE_AGENTCORE_USAGE_POLL_INTERVAL_S'] = '10'
    from atsuite.mcp import mcp
    mcp._MCP_CLIENT_TIMEOUT_SECONDS = 60.0
    spec = experiment_spec()
    preview = service.plan(spec)
    from atsuite.market_data.service import freeze_market_data
    policy = preview['spec']['market_data']
    snapshot = json.loads(args.market_snapshot.read_text()) if args.market_snapshot else None
    market = freeze_market_data(preview['candidates'], {**policy, 'pricing': 'snapshot'} if snapshot else policy,
                                approved_snapshot=snapshot)
    if market['status'] != 'frozen':
        raise ValueError('Fresh, complete official market data required')
    validate_runtime_bound(market)
    market['operation_cost_bounds'] = dict(BOUNDS)
    market['operation_cost_bound_assumptions'] = {
        'currency': 'CNY', 'scope': 'incremental smoke experiment, excludes previous/shared charges',
        'build': 'local Docker, <=500 MB new image/provider, 24-hour registry reserve',
        'invoke': '3 calls; <=3 AgentCore sessions, lifetime <=90s each; <=8 vCPU/32GB; <=5 MB logs/1 MB transfer',
        'billing_limit': 'dispatch estimate, not a provider hard spending cap',
    }
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    plan = service.plan(spec, output / 'plan.json', market_snapshot=market)
    if len(plan['candidates']) != 4 or {c['provider'] for c in plan['candidates']} != set(PROVIDERS):
        raise ValueError('Expected exactly one candidate per provider')
    if not args.confirm_live:
        print(json.dumps({'status': 'plan_only', 'plan': str(output / 'plan.json'), 'workflow_trials': 4,
                          'reserved_cny': str(4 * sum(map(Decimal, BOUNDS.values())))}))
        return 0
    accounts = {p: os.environ['AWS_ACCOUNT_ID' if p.startswith('aws') else 'ALI_ACCOUNT_ID'] for p in PROVIDERS}
    store = open_store(output / 'jobs.sqlite')
    try:
        approval = service.approve(store, plan, subject='local-cli', tenant='local', project=output.name, accounts=accounts)
        record = service.enqueue(store, plan, approval, subject='local-cli', tenant='local', project=output.name)
        print(json.dumps({'experiment_id': record['id'], 'log': str(output / 'run.log')}), flush=True)
        with (output / 'run.log').open('w', buffering=1) as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            result = service.run(store, record['id'], backend=BudgetBackend())
            deadline = time.monotonic() + 300
            while result['state'] == 'CLEANUP_PENDING' and time.monotonic() < deadline:
                time.sleep(2)
                result = service.run(store, record['id'], backend=BudgetBackend())
        summary = {k: result.get(k) for k in ('id', 'state', 'cleanup_status', 'cleanup_error', 'error', 'report_path', 'recommendation', 'trials')}
        (output / 'result.json').write_text(json.dumps(summary, indent=2, default=str) + '\n')
        print(json.dumps({'state': result['state'], 'paid_trials': len(result.get('trials', [])), 'result': str(output / 'result.json')}))
        return 0 if result['state'] == 'COMPLETED' else 1
    finally:
        store.close()


if __name__ == '__main__':
    raise SystemExit(main())
