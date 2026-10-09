from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from atsuite.execution.artifacts import ArtifactStore
from atsuite.execution.postgres_store import open_store
from atsuite.recommender.contracts import ExperimentSpec, utc_now
from atsuite.recommender.identity import digest
from atsuite.server.auth import Principal, TokenIdentityProvider
from atsuite.server.security import confined_path, safe_extract_zip
import atsuite.server.security as security


def create_app(*, store_path: str | Path = ':memory:', artifact_root: str | Path = 'results/recommendations',
               web_root: str | Path | None = None, mode='local_development', identity_tokens=None,
               execution_backend=None,
               production_adapter=None) -> FastAPI:
    if identity_tokens is None and os.environ.get('ATSUITE_IDENTITY_TOKENS'):
        identity_tokens = json.loads(os.environ['ATSUITE_IDENTITY_TOKENS'])
    identities = TokenIdentityProvider(identity_tokens, mode=mode)
    if execution_backend is None:
        from atsuite.execution.cloud_backend import CloudBackend
        execution_backend = CloudBackend()
    if mode == 'production' and (not getattr(execution_backend, 'isolated_builder', False)
            or not getattr(execution_backend, 'ownership_cleanup', False) or getattr(execution_backend, 'simulated', False)):
        raise ValueError('production requires isolated builder and ownership-aware cleanup capability')
    app = FastAPI(title='ATSuite Recommender', version='2')
    root = Path(artifact_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    store = open_store(store_path)
    artifacts = ArtifactStore(root)
    benchmark_root = root / 'benchmarks'
    benchmark_root.mkdir(exist_ok=True)
    app.state.store = store
    app.state.execution_backend = execution_backend
    if production_adapter is None and os.environ.get('ATSUITE_PRODUCTION_TARGETS'):
        from atsuite.server.production import AWSLambdaProductionAdapter
        production_adapter = AWSLambdaProductionAdapter(json.loads(os.environ['ATSUITE_PRODUCTION_TARGETS']))
    app.state.production_adapter = production_adapter
    app.state.apply_plans = {}
    app.state.apply_approvals = {}

    @app.middleware('http')
    async def authenticate_and_audit(request, call_next):
        if not request.url.path.startswith('/api/'):
            return await call_next(request)
        try:
            request.state.principal = identities.guard(request, mutate=request.method not in {'GET', 'HEAD', 'OPTIONS'})
            response = await call_next(request)
        except HTTPException as exc:
            response = JSONResponse({'detail': exc.detail}, status_code=exc.status_code)
        except (ValueError, TypeError, KeyError):
            response = JSONResponse({'detail': 'invalid request contract'}, status_code=400)
        except Exception:
            response = JSONResponse({'detail': 'control-plane request failed; consult audit record'}, status_code=500)
        identity = getattr(request.state, 'principal', None)
        audit = {'at': utc_now(), 'subject': identity.subject if identity else 'unauthenticated',
                 'tenant': identity.tenant if identity else None, 'project': identity.project if identity else None,
                 'method': request.method, 'path': request.url.path, 'status': response.status_code}
        # Request bodies, bearer tokens, model prompts and query values are not audited.
        with (root / 'audit.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(audit, ensure_ascii=False) + '\n')
        return response

    def who(request) -> Principal:
        return request.state.principal

    async def json_payload(request):
        limit = 5 * 1024 * 1024
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > limit:
                raise HTTPException(413, 'JSON request exceeds size limit')
            body.extend(chunk)
        try:
            value = json.loads(body)
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(400, 'invalid JSON request') from exc
        if not isinstance(value, dict):
            raise HTTPException(400, 'JSON request must be an object')
        return value

    def owned(experiment_id, identity):
        try:
            record = store.get(experiment_id)
        except KeyError as exc:
            raise HTTPException(404, 'experiment not found') from exc
        if (record.get('tenant'), record.get('project')) != (identity.tenant, identity.project):
            raise HTTPException(404, 'experiment not found')
        return record

    def register_artifact(record, name, value, identity):
        relative = f"{record['id']}/{name}"
        path = artifacts.write_json(relative, value)
        artifact_id = 'artifact-' + digest({'experiment': record['id'], 'path': relative}, length=24)
        store.register_artifact(artifact_id, identity.tenant, identity.project, str(path.relative_to(root)))
        return artifact_id

    def web_spec(payload, identity):
        payload = dict(payload)
        benchmark = payload.get('benchmark') or {}
        if not isinstance(benchmark, dict) or set(benchmark) - {'benchmark_id', 'config_id', 'correctness_policy'}:
            raise HTTPException(400, 'Web requires benchmark_id and config_id; server paths are not accepted')
        benchmark_id, config_id = benchmark.get('benchmark_id'), benchmark.get('config_id')
        if not benchmark_id or not config_id:
            raise HTTPException(400, 'benchmark_id and config_id are required')
        try:
            registered = store.benchmark(benchmark_id, identity.tenant, identity.project)
        except KeyError as exc:
            raise HTTPException(404, 'benchmark not found') from exc
        configs = {row['config_id']: row['relative_path'] for row in registered['configs']}
        if config_id not in configs:
            raise HTTPException(404, 'config not found')
        bundle_root = Path(registered['root']).resolve()
        config = confined_path(bundle_root, configs[config_id], bundle_root)
        # Web does not expose local profiles or environment/module configuration.
        if payload.get('provider_profiles') or payload.get('benchmark_config'):
            raise HTTPException(400, 'server path references are not accepted')
        advisor = payload.get('advisor') or {}
        if advisor.get('model_from_env', 'ATSUITE_ADVISOR_MODEL') != 'ATSUITE_ADVISOR_MODEL':
            raise HTTPException(400, 'model configuration is server managed')
        payload['benchmark'] = {'config': str(config), 'correctness_policy': benchmark.get('correctness_policy', 'require_declared_oracle')}
        # This field expresses intent, and never acts as approval.
        payload['authorization'] = 'plan_only'
        try:
            return ExperimentSpec.from_dict(payload)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    def public_benchmark(record):
        return {k: v for k, v in record.items() if k not in {'root', 'config_files'}}

    @app.get('/api/v1/principal')
    def principal(request: Request):
        return who(request).public()

    @app.post('/api/v1/benchmarks', status_code=201)
    async def upload(request: Request):
        identity = who(request)
        if not request.headers.get('x-filename', 'benchmark.zip').lower().endswith('.zip'):
            raise HTTPException(400, 'only ZIP packages are accepted')
        length = request.headers.get('content-length')
        if length and (not length.isdecimal() or int(length) > security.MAX_ARCHIVE_BYTES):
            raise HTTPException(413, 'benchmark archive exceeds compressed size limit')
        temporary = None
        try:
            total = 0
            archive_digest = hashlib.sha256()
            with tempfile.NamedTemporaryFile(suffix='.zip', dir=root, delete=False) as handle:
                temporary = Path(handle.name)
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > security.MAX_ARCHIVE_BYTES:
                        raise HTTPException(413, 'benchmark archive exceeds compressed size limit')
                    handle.write(chunk)
                    archive_digest.update(chunk)
            if not total:
                raise HTTPException(400, 'benchmark archive is empty')
            benchmark_id = 'benchmark-' + digest({'tenant': identity.tenant, 'project': identity.project,
                                                  'sha256': archive_digest.hexdigest()}, length=24)
            try:
                return public_benchmark(store.benchmark(benchmark_id, identity.tenant, identity.project))
            except KeyError:
                pass
            # Scope is opaque; tenant/project never become filesystem components.
            destination = benchmark_root / benchmark_id
            metadata = safe_extract_zip(temporary, destination)
            configs = [{'config_id': 'config-' + digest(path, length=20), 'relative_path': path}
                       for path in metadata['config_files']]
            record = store.register_benchmark(benchmark_id, identity.tenant, identity.project,
                                             {'created_at': utc_now(), 'root': str(destination), 'configs': configs, **metadata})
            return public_benchmark(record)
        except (ValueError, OSError, __import__('zipfile').BadZipFile) as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    @app.post('/api/v1/experiments/plan')
    async def preview(request: Request):
        from atsuite.recommender import service
        try:
            return service.plan(web_spec(await json_payload(request), who(request)))
        except (ValueError, FileNotFoundError, SystemExit) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post('/api/v1/experiments', status_code=202)
    async def create(request: Request):
        from atsuite.recommender import service
        identity = who(request)
        payload = await json_payload(request)
        spec = web_spec(payload, identity)
        experiment_id = 'exp-' + secrets.token_hex(10)
        key = request.headers.get('idempotency-key')
        if key:
            try:
                selected = store.claim_idempotency(f'{identity.tenant}/{identity.project}/experiment', key,
                                                  digest(payload, length=64), experiment_id)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            if selected != experiment_id:
                return owned(selected, identity)
        try:
            planned = service.plan(spec, output=root / experiment_id / 'plan.json')
        except (ValueError, FileNotFoundError, SystemExit) as exc:
            raise HTTPException(400, str(exc)) from exc
        record = {'id': experiment_id, 'tenant': identity.tenant, 'project': identity.project,
                  'subject': identity.subject, 'plan': planned, 'plan_digest': service.plan_digest(planned),
                  'artifact_root': str(root / 'execution'),
                  'completed_stages': [], 'created_at': utc_now()}
        record['plan_artifact_id'] = register_artifact(record, 'plan.json', planned, identity)
        return store.create(experiment_id, record, state='PLANNED')

    @app.get('/api/v1/experiments/{experiment_id}')
    def get(experiment_id: str, request: Request):
        record = owned(experiment_id, who(request))
        if record['state'] == 'APPROVED':
            record['worker_status'] = 'queued_waiting_for_worker'
        return record

    @app.post('/api/v1/experiments/{experiment_id}/approve')
    async def approve(experiment_id: str, request: Request):
        from atsuite.recommender import service
        identity = who(request)
        record = owned(experiment_id, identity)
        body = await json_payload(request)
        if body.get('confirm_budget') is not True or not body.get('plan_digest') or body['plan_digest'] != record['plan_digest']:
            raise HTTPException(409, 'explicit budget confirmation and matching frozen plan digest are required')
        try:
            approval = service.approve(store, record['plan'], subject=identity.subject,
                                       tenant=identity.tenant, project=identity.project, accounts=body.get('accounts'))
            return store.transition(experiment_id, record['state'], approval=approval)
        except (ValueError, PermissionError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/v1/experiments/{experiment_id}/run', status_code=202)
    def run(experiment_id: str, request: Request):
        from atsuite.recommender import service
        identity = who(request)
        record = owned(experiment_id, identity)
        if not record.get('approval'):
            raise HTTPException(403, 'server-validated plan and budget approval is required')
        try:
            return service.enqueue(store, record['plan'], record['approval'], tenant=identity.tenant,
                                   project=identity.project, subject=identity.subject, experiment_id=experiment_id)
        except (ValueError, PermissionError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/v1/experiments/{experiment_id}/events')
    def events(experiment_id: str, request: Request, after: int = 0):
        owned(experiment_id, who(request))
        if after < 0:
            raise HTTPException(400, 'event cursor must be nonnegative')
        rows = store.events(experiment_id, after)
        # Trial records remain in canonical storage and export. Progress carries
        # references/counts, avoiding a quadratic series of trial-array copies.
        public_rows = []
        for row in rows:
            summary = {key: row[key] for key in ('id', 'event', 'state', 'stage', 'stage_status',
                       'message', 'error', 'cleanup_status', 'trial_count', 'completed_attempts') if key in row}
            if isinstance(row.get('trials'), list):
                summary['completed_attempts'] = sum(trial.get('status') in {'success', 'failed', 'timeout'}
                                                    for trial in row['trials'])
            public_rows.append(summary)
        return {'events': public_rows, 'cursor': rows[-1]['id'] if rows else after, 'poll_after_ms': 1000}

    @app.post('/api/v1/experiments/{experiment_id}/cancel')
    def cancel(experiment_id: str, request: Request):
        owned(experiment_id, who(request))
        try:
            return store.transition(experiment_id, 'CANCEL_REQUESTED')
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/v1/experiments/{experiment_id}/resume', status_code=202)
    def resume(experiment_id: str, request: Request):
        from atsuite.recommender import service
        record = owned(experiment_id, who(request))
        if not record.get('approval'):
            raise HTTPException(403, 'server-validated approval is required before resume')
        try:
            return service.resume(store, experiment_id)
        except (ValueError, PermissionError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/v1/experiments/{experiment_id}/comparison')
    def comparison(experiment_id: str, request: Request):
        record = owned(experiment_id, who(request))
        return record.get('comparison') or {'schema_version': 2, 'status': 'waiting_for_evidence', 'candidates': []}

    @app.get('/api/v1/experiments/{experiment_id}/recommendation')
    def recommendation(experiment_id: str, request: Request):
        record = owned(experiment_id, who(request))
        return record.get('recommendation') or {'schema_version': 2, 'status': 'waiting_for_evidence',
                                               'best_verified': None, 'scope': record['plan'].get('scope', {})}

    @app.get('/api/v1/experiments/{experiment_id}/sources')
    def sources(experiment_id: str, request: Request):
        record = owned(experiment_id, who(request))
        return {name: record.get(name) for name in ('market_snapshot', 'fx_snapshot', 'price_coverage',
                                                   'prediction_audit', 'search_audit', 'model_registry', 'model_evaluation', 'cleanup_status')}

    @app.get('/api/v1/experiments/{experiment_id}/evidence/{reference}')
    def evidence_artifact(experiment_id: str, reference: str, request: Request):
        from atsuite.recommender.eligibility import EvidenceResolver
        record = owned(experiment_id, who(request))
        rows = record.get('evidence_candidates') or (record.get('comparison') or {}).get('evidence_candidates') or []
        for row in rows:
            if reference in (row.get('artifacts') or {}):
                try:
                    return EvidenceResolver(row['artifacts']).resolve(reference, row)
                except ValueError as exc:
                    raise HTTPException(409, 'evidence reference failed integrity verification') from exc
        raise HTTPException(404, 'evidence reference not found')

    @app.post('/api/v1/experiments/{experiment_id}/reprice', status_code=201)
    async def reprice(experiment_id: str, request: Request):
        identity = who(request)
        record = owned(experiment_id, identity)
        snapshot = await json_payload(request)
        if not isinstance(snapshot, dict) or isinstance(snapshot.get('fx') or snapshot.get('fx_snapshot'), str):
            raise HTTPException(400, 'Web repricing requires inline FX data; server file paths are not accepted')
        from atsuite.market_data.reprice import reprice as calculate
        ledger_ref = record.get('usage_ledger') or f'{experiment_id}/usage_ledger.jsonl'
        try:
            reference = Path(str(ledger_ref))
            if reference.is_absolute():
                ledger = reference.resolve()
                owned_root = (root / 'execution' / experiment_id).resolve()
                if owned_root not in ledger.parents or not ledger.is_file():
                    raise ValueError('ledger is outside owned execution artifacts')
            else:
                ledger = confined_path(root, str(ledger_ref), root)
        except ValueError:
            result = {'schema_version': 2, 'status': 'insufficient_usage_ledger', 'market_snapshot_digest': digest(snapshot, length=32)}
        else:
            result = calculate(record, snapshot, usage_ledger_path=ledger,
                               target_currency=record['plan']['spec']['objective']['base_currency'])
        version_id = 'reprice-' + digest(result, length=24)
        artifact_id = register_artifact(record, f'repricings/{version_id}.json', result, identity)
        versions = list(record.get('repricings') or [])
        versions.append({'id': version_id, 'status': result['status'], 'artifact_id': artifact_id,
                         'created_at': utc_now(), 'result': result})
        return store.transition(experiment_id, record['state'], repricings=versions)

    @app.post('/api/v1/experiments/{experiment_id}/advisor')
    async def advisor(experiment_id: str, request: Request):
        from atsuite.advisor.client import OpenAIAdvisor
        from atsuite.advisor.verify import verify_response
        from atsuite.advisor.workflow import explanation_catalog
        from atsuite.execution.budget import BudgetController
        record = owned(experiment_id, who(request))
        body = await json_payload(request)
        recommendation_data = record.get('recommendation') or {}
        comparison_data = record.get('comparison') or {}
        rows = record.get('evidence_candidates') or comparison_data.get('evidence_candidates') or []
        catalog = explanation_catalog(rows, recommendation_data)
        candidates = catalog['candidates']
        eligible = set(catalog['eligible_candidates'])
        spec = record['plan']['spec']
        model = ''
        if record.get('approval') and spec.get('advisor', {}).get('enabled'):
            from atsuite.recommender.service import validate_approval
            validate_approval(store, record['plan'], record['approval'], tenant=record['tenant'], project=record['project'])
            model = os.environ.get('ATSUITE_ADVISOR_MODEL', '')
        response = OpenAIAdvisor(model=model, budget_controller=BudgetController(store, experiment_id)).recommend(catalog, str(body.get('prompt') or ''))
        return verify_response(response, candidate_catalog=candidates, eligible_ids=eligible,
                               metric_catalog=catalog['metrics'], objective_winner=catalog['objective_winner']).to_dict()

    @app.get('/api/v1/providers/capabilities')
    def capabilities():
        from dataclasses import asdict
        from atsuite.pipeline import PROVIDERS
        from atsuite.recommender.capabilities import describe_capability
        return {provider: asdict(describe_capability(provider, 'default')) for provider in PROVIDERS}

    @app.get('/api/v1/artifacts/{artifact_id}')
    def artifact(artifact_id: str, request: Request):
        identity = who(request)
        try:
            metadata = store.artifact(artifact_id, identity.tenant, identity.project)
            path = confined_path(root, metadata['relative_path'], root)
        except (KeyError, ValueError) as exc:
            raise HTTPException(404, 'artifact not found') from exc
        if not path.is_file():
            raise HTTPException(404, 'artifact not found')
        return FileResponse(path, media_type=metadata['media_type'], filename=path.name)

    @app.get('/api/v1/experiments/{experiment_id}/export')
    def export(experiment_id: str, request: Request, format: str = 'json'):
        record = owned(experiment_id, who(request))
        if format == 'json':
            return JSONResponse(record, headers={'Content-Disposition': f'attachment; filename="{experiment_id}.json"'})
        if format != 'markdown':
            raise HTTPException(400, 'format must be json or markdown')
        body = f"# ATSuite experiment {experiment_id}\n\nStatus: {record['state']}\n\n```json\n{json.dumps(record, ensure_ascii=False, indent=2)}\n```\n"
        return Response(body, media_type='text/markdown', headers={'Content-Disposition': f'attachment; filename="{experiment_id}.md"'})

    @app.post('/api/v1/deployments/plan', status_code=201)
    async def deployment_plan(request: Request):
        from atsuite.server.production import prepare_change
        identity = who(request)
        if identity.role != 'admin':
            raise HTTPException(403, 'production admin role required')
        if app.state.production_adapter is None:
            raise HTTPException(503, 'production adapter is not configured')
        body = await json_payload(request)
        record = owned(body.get('experiment_id', ''), identity)
        try:
            change = prepare_change(record, body.get('candidate_id', ''), body.get('target_id', ''),
                                    app.state.production_adapter, tenant=identity.tenant, project=identity.project)
        except (ValueError, PermissionError) as exc:
            raise HTTPException(409, str(exc)) from exc
        app.state.apply_plans[change['deployment_plan_id']] = change
        register_artifact(record, f"deployments/{change['deployment_plan_id']}.json", change, identity)
        return change

    @app.post('/api/v1/deployments/approve', status_code=201)
    async def deployment_approve(request: Request):
        identity = who(request)
        if identity.role != 'admin':
            raise HTTPException(403, 'production admin role required')
        body = await json_payload(request)
        change = app.state.apply_plans.get(body.get('deployment_plan_id'))
        if change is None or (change['tenant'], change['project']) != (identity.tenant, identity.project):
            raise HTTPException(404, 'deployment plan not found')
        if body.get('confirm') is not True or body.get('config_digest') != change['config_digest']:
            raise HTTPException(409, 'explicit matching production change approval is required')
        approval = {'approval_id': 'production-' + secrets.token_hex(24), 'subject': identity.subject,
                    'deployment_plan_id': change['deployment_plan_id'], 'config_digest': change['config_digest']}
        app.state.apply_approvals[approval['approval_id']] = approval
        return approval

    @app.post('/api/v1/deployments/apply')
    async def deployment_apply(request: Request):
        from atsuite.server.production import apply_change
        identity = who(request)
        if identity.role != 'admin':
            raise HTTPException(403, 'production admin role required')
        body = await json_payload(request)
        approval = app.state.apply_approvals.pop(body.get('approval_id'), None)
        if not approval or approval['subject'] != identity.subject:
            raise HTTPException(403, 'valid additional production approval is required')
        change = app.state.apply_plans.get(approval['deployment_plan_id'])
        if not change or (change['tenant'], change['project']) != (identity.tenant, identity.project):
            raise HTTPException(404, 'deployment plan not found')
        record = owned(change['experiment_id'], identity)
        try:
            result = apply_change(change, approval, app.state.production_adapter)
        except (PermissionError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        result['artifact_id'] = register_artifact(record, f"deployments/{change['deployment_plan_id']}-result.json", result, identity)
        return result

    ui = Path(web_root).resolve() if web_root else Path(__file__).resolve().parents[2] / 'web' / 'recommender'
    if ui.is_dir():
        app.mount('/', StaticFiles(directory=ui, html=True), name='ui')
    return app
