from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from atsuite.advisor.client import OpenAIAdvisor
from atsuite.advisor.verify import verify_response
from atsuite.execution.artifacts import ArtifactStore
from atsuite.execution.store import JobStore
from atsuite.recommender.api import plan_experiment
from atsuite.recommender.contracts import ExperimentSpec, utc_now
from atsuite.recommender.identity import digest
from atsuite.recommender.search import SearchOrchestrator
from atsuite.server.security import safe_extract_zip


def create_app(*, store_path: str | Path = ":memory:", artifact_root: str | Path = "results/recommendations",
               web_root: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="ATSuite Recommender", version="1")
    store = JobStore(store_path)
    root = Path(artifact_root).resolve()
    artifacts = ArtifactStore(root)
    benchmark_root = root / "benchmarks"
    benchmark_root.mkdir(parents=True, exist_ok=True)
    orchestrator = SearchOrchestrator(store)

    def principal(authorization: str | None, tenant: str, project: str, role: str) -> tuple[str, str, str]:
        expected = os.environ.get("ATSUITE_API_TOKEN")
        if expected and not secrets.compare_digest(authorization or "", f"Bearer {expected}"):
            raise HTTPException(401, "invalid authorization")
        if role not in {"viewer", "editor", "admin"}:
            raise HTTPException(403, "invalid role")
        return tenant.strip() or "default", project.strip() or "default", role

    def guard(request: Request, authorization: str | None, tenant: str, project: str, role: str, *, mutate: bool = False):
        identity = principal(authorization, tenant, project, role)
        if mutate:
            if identity[2] not in {"editor", "admin"}:
                raise HTTPException(403, "write role required")
            origin = request.headers.get("origin")
            if origin:
                configured = {item.strip() for item in os.environ.get("ATSUITE_ALLOWED_ORIGINS", "").split(",") if item.strip()}
                same_origin = f"{request.url.scheme}://{request.url.netloc}"
                if origin != same_origin and origin not in configured:
                    raise HTTPException(403, "origin is not allowed")
        return identity

    def owned(experiment_id: str, identity: tuple[str, str, str]) -> dict[str, Any]:
        try:
            record = store.get(experiment_id)
        except KeyError as exc:
            raise HTTPException(404, "experiment not found") from exc
        if (record.get("tenant"), record.get("project")) != identity[:2]:
            raise HTTPException(404, "experiment not found")
        return record

    def headers(authorization, x_atsuite_tenant, x_atsuite_project, x_atsuite_role):
        return authorization, x_atsuite_tenant, x_atsuite_project, x_atsuite_role

    @app.get("/api/v1/providers/capabilities")
    def capabilities(request: Request, authorization: str | None = Header(default=None), x_atsuite_tenant: str = Header(default="default"),
                     x_atsuite_project: str = Header(default="default"), x_atsuite_role: str = Header(default="viewer")):
        guard(request, *headers(authorization, x_atsuite_tenant, x_atsuite_project, x_atsuite_role))
        from dataclasses import asdict
        from atsuite.pipeline import PROVIDERS
        from atsuite.recommender.capabilities import describe_capability
        return {provider: asdict(describe_capability(provider, "default")) for provider in PROVIDERS}

    @app.post("/api/v1/benchmarks", status_code=201)
    async def upload_benchmark(request: Request, authorization: str | None = Header(default=None), x_atsuite_tenant: str = Header(default="default"),
                               x_atsuite_project: str = Header(default="default"), x_atsuite_role: str = Header(default="editor"),
                               x_filename: str = Header(default="benchmark.zip")):
        identity = guard(request, *headers(authorization, x_atsuite_tenant, x_atsuite_project, x_atsuite_role), mutate=True)
        if not x_filename.lower().endswith(".zip"):
            raise HTTPException(400, "only .zip benchmark packages are accepted")
        body = await request.body()
        if not body:
            raise HTTPException(400, "benchmark archive is empty")
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as handle:
            handle.write(body)
            temporary = Path(handle.name)
        try:
            archive_hash = hashlib.sha256(body).hexdigest()
            benchmark_id = "benchmark-" + digest({"tenant": identity[0], "project": identity[1], "sha256": archive_hash}, length=24)
            destination = benchmark_root / identity[0] / identity[1] / benchmark_id
            if destination.exists():
                try:
                    return store.benchmark(benchmark_id, identity[0], identity[1])
                except KeyError:
                    pass
            metadata = safe_extract_zip(temporary, destination)
            if not metadata["config_files"]:
                raise ValueError("benchmark package contains no config JSON under a config directory")
            return store.register_benchmark(benchmark_id, identity[0], identity[1], {"created_at": utc_now(), "root": str(destination), **metadata})
        except (ValueError, OSError, __import__("zipfile").BadZipFile) as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            temporary.unlink(missing_ok=True)

    @app.post("/api/v1/experiments/plan")
    async def plan(request: Request, authorization: str | None = Header(default=None), x_atsuite_tenant: str = Header(default="default"),
                   x_atsuite_project: str = Header(default="default"), x_atsuite_role: str = Header(default="editor")):
        guard(request, *headers(authorization, x_atsuite_tenant, x_atsuite_project, x_atsuite_role), mutate=True)
        try:
            return plan_experiment(ExperimentSpec.from_dict(await request.json()))
        except (ValueError, FileNotFoundError, SystemExit) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/v1/experiments", status_code=202)
    async def create_experiment(request: Request, authorization: str | None = Header(default=None), idempotency_key: str | None = Header(default=None),
                                x_atsuite_tenant: str = Header(default="default"), x_atsuite_project: str = Header(default="default"),
                                x_atsuite_role: str = Header(default="editor")):
        identity = guard(request, *headers(authorization, x_atsuite_tenant, x_atsuite_project, x_atsuite_role), mutate=True)
        payload = await request.json()
        try:
            spec = ExperimentSpec.from_dict(payload)
            planned = plan_experiment(spec)
        except (ValueError, FileNotFoundError, SystemExit) as exc:
            raise HTTPException(400, str(exc)) from exc
        request_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        proposed = "exp-" + secrets.token_hex(10)
        if idempotency_key:
            try:
                experiment_id = store.claim_idempotency(f"{identity[0]}/{identity[1]}/experiment", idempotency_key, request_hash, proposed)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            if experiment_id != proposed:
                return owned(experiment_id, identity)
        else:
            experiment_id = proposed
        plan_ref = f"{experiment_id}/plan.json"
        plan_path = artifacts.write_json(plan_ref, planned)
        artifact_id = "artifact-" + digest({"experiment": experiment_id, "path": plan_ref}, length=24)
        store.register_artifact(artifact_id, identity[0], identity[1], str(plan_path.relative_to(root)))
        initial_state = "APPROVED" if spec.authorization in {"approved_execution", "production_apply"} else "PLANNED"
        return store.create(experiment_id, {"tenant": identity[0], "project": identity[1], "plan": planned,
                                            "plan_artifact_id": artifact_id, "completed_stages": [], "created_at": utc_now()}, state=initial_state)

    @app.get("/api/v1/experiments/{experiment_id}")
    def get_experiment(experiment_id: str, request: Request, authorization: str | None = Header(default=None), x_atsuite_tenant: str = Header(default="default"),
                       x_atsuite_project: str = Header(default="default"), x_atsuite_role: str = Header(default="viewer")):
        return owned(experiment_id, guard(request, *headers(authorization, x_atsuite_tenant, x_atsuite_project, x_atsuite_role)))

    @app.get("/api/v1/experiments/{experiment_id}/events")
    def events(experiment_id: str, request: Request, authorization: str | None = Header(default=None), last_event_id: int = Header(default=0),
               x_atsuite_tenant: str = Header(default="default"), x_atsuite_project: str = Header(default="default"), x_atsuite_role: str = Header(default="viewer")):
        identity = guard(request, *headers(authorization, x_atsuite_tenant, x_atsuite_project, x_atsuite_role))
        owned(experiment_id, identity)
        def stream():
            rows = store.events(experiment_id, last_event_id)
            for event in rows:
                yield f"id: {event['id']}\nevent: {event.get('event', 'progress')}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            if not rows:
                yield ": heartbeat\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.post("/api/v1/experiments/{experiment_id}/cancel")
    def cancel(experiment_id: str, request: Request, authorization: str | None = Header(default=None), x_atsuite_tenant: str = Header(default="default"),
               x_atsuite_project: str = Header(default="default"), x_atsuite_role: str = Header(default="editor")):
        identity = guard(request, *headers(authorization, x_atsuite_tenant, x_atsuite_project, x_atsuite_role), mutate=True)
        owned(experiment_id, identity)
        try:
            return orchestrator.transition(experiment_id, "CANCEL_REQUESTED")
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/v1/experiments/{experiment_id}/resume", status_code=202)
    def resume(experiment_id: str, request: Request, authorization: str | None = Header(default=None), x_atsuite_tenant: str = Header(default="default"),
               x_atsuite_project: str = Header(default="default"), x_atsuite_role: str = Header(default="editor")):
        identity = guard(request, *headers(authorization, x_atsuite_tenant, x_atsuite_project, x_atsuite_role), mutate=True)
        record = owned(experiment_id, identity)
        if record.get("plan", {}).get("spec", {}).get("authorization") == "plan_only":
            raise HTTPException(409, "plan_only experiments require a new approved execution plan")
        try:
            return orchestrator.resume(experiment_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/v1/experiments/{experiment_id}/comparison")
    def comparison(experiment_id: str, request: Request, authorization: str | None = Header(default=None), x_atsuite_tenant: str = Header(default="default"),
                   x_atsuite_project: str = Header(default="default"), x_atsuite_role: str = Header(default="viewer")):
        record = owned(experiment_id, guard(request, *headers(authorization, x_atsuite_tenant, x_atsuite_project, x_atsuite_role)))
        return record.get("comparison") or {"schema_version": 1, "status": "insufficient_evidence", "candidates": record["plan"].get("candidates", [])}

    @app.get("/api/v1/experiments/{experiment_id}/recommendation")
    def recommendation(experiment_id: str, request: Request, authorization: str | None = Header(default=None), x_atsuite_tenant: str = Header(default="default"),
                       x_atsuite_project: str = Header(default="default"), x_atsuite_role: str = Header(default="viewer")):
        record = owned(experiment_id, guard(request, *headers(authorization, x_atsuite_tenant, x_atsuite_project, x_atsuite_role)))
        return record.get("recommendation") or {"schema_version": 1, "status": "insufficient_evidence", "best_verified": None,
                                                 "scope": record["plan"].get("scope", {})}

    @app.post("/api/v1/experiments/{experiment_id}/reprice", status_code=201)
    async def reprice(experiment_id: str, request: Request, authorization: str | None = Header(default=None), x_atsuite_tenant: str = Header(default="default"),
                      x_atsuite_project: str = Header(default="default"), x_atsuite_role: str = Header(default="editor")):
        identity = guard(request, *headers(authorization, x_atsuite_tenant, x_atsuite_project, x_atsuite_role), mutate=True)
        record = owned(experiment_id, identity)
        snapshot = await request.json()
        from atsuite.market_data.reprice import reprice as calculate_reprice
        ledger_ref = str(record.get("usage_ledger") or f"{experiment_id}/usage_ledger.jsonl")
        ledger_path = (root / ledger_ref).resolve()
        if root not in ledger_path.parents or not ledger_path.is_file():
            result = {
                "schema_version": 1,
                "status": "insufficient_usage_ledger",
                "market_snapshot_digest": digest(snapshot, length=32),
                "usage_ledger": ledger_ref,
            }
        else:
            result = calculate_reprice(
                record, snapshot, usage_ledger_path=ledger_path,
                target_currency=str(((record.get("plan") or {}).get("spec") or {}).get("objective", {}).get("base_currency", "USD")),
            )
        reprice_id = "reprice-" + digest(result, length=20)
        relative = f"{experiment_id}/repricings/{reprice_id}.json"
        result_path = artifacts.write_json(relative, result)
        artifact_id = "artifact-" + digest({"experiment": experiment_id, "path": relative}, length=24)
        store.register_artifact(artifact_id, identity[0], identity[1], str(result_path.relative_to(root)))
        versions = list(record.get("repricings") or ())
        versions.append({"id": reprice_id, "created_at": utc_now(), "status": result["status"],
                         "artifact_id": artifact_id,
                         "market_snapshot_digest": result.get("market_snapshot_digest", ""),
                         "usage_ledger_digest": result.get("usage_ledger_digest", "")})
        return store.transition(experiment_id, record["state"], repricings=versions)

    @app.post("/api/v1/experiments/{experiment_id}/advisor")
    async def advisor(experiment_id: str, request: Request, authorization: str | None = Header(default=None), x_atsuite_tenant: str = Header(default="default"),
                      x_atsuite_project: str = Header(default="default"), x_atsuite_role: str = Header(default="editor")):
        identity = guard(request, *headers(authorization, x_atsuite_tenant, x_atsuite_project, x_atsuite_role), mutate=True)
        record = owned(experiment_id, identity)
        body = await request.json()
        recommendation_data = record.get("recommendation") or {}
        comparison_data = record.get("comparison") or {}
        candidates = {str(row.get("candidate_id")): row for row in record["plan"].get("candidates", [])}
        catalog = {"candidates": candidates, "eligible_candidates": list(candidates), "metrics": comparison_data.get("metrics", {}),
                   "objective_winner": recommendation_data.get("best_verified"), "decision_status": recommendation_data.get("status")}
        response = OpenAIAdvisor().recommend(catalog, str(body.get("prompt") or ""))
        checked = verify_response(response, candidate_catalog=candidates, eligible_ids=set(candidates), metric_catalog=catalog["metrics"],
                                  objective_winner=catalog["objective_winner"])
        return checked.to_dict()

    @app.post("/api/v1/deployments/plan", status_code=201)
    async def deployment_plan(request: Request, authorization: str | None = Header(default=None), x_atsuite_tenant: str = Header(default="default"),
                              x_atsuite_project: str = Header(default="default"), x_atsuite_role: str = Header(default="editor")):
        identity = guard(request, *headers(authorization, x_atsuite_tenant, x_atsuite_project, x_atsuite_role), mutate=True)
        body = await request.json()
        record = owned(str(body.get("experiment_id") or ""), identity)
        candidate_id = str(body.get("candidate_id") or (record.get("recommendation") or {}).get("best_verified") or "")
        if not candidate_id:
            raise HTTPException(409, "no verified candidate is available")
        return {"schema_version": 1, "deployment_plan_id": "deployment-plan-" + digest({"experiment": record["id"], "candidate": candidate_id}, length=24),
                "experiment_id": record["id"], "candidate_id": candidate_id, "status": "dry_run", "requires_apply_confirmation": True}

    @app.post("/api/v1/deployments/apply", status_code=202)
    async def deployment_apply(request: Request, authorization: str | None = Header(default=None), x_atsuite_tenant: str = Header(default="default"),
                               x_atsuite_project: str = Header(default="default"), x_atsuite_role: str = Header(default="admin")):
        identity = guard(request, *headers(authorization, x_atsuite_tenant, x_atsuite_project, x_atsuite_role), mutate=True)
        if identity[2] != "admin":
            raise HTTPException(403, "admin role required")
        body = await request.json()
        if body.get("confirm") is not True:
            raise HTTPException(400, "confirm=true is required")
        record = owned(str(body.get("experiment_id") or ""), identity)
        if record.get("plan", {}).get("spec", {}).get("authorization") != "production_apply":
            raise HTTPException(403, "experiment does not authorize production apply")
        return {"status": "queued", "experiment_id": record["id"], "deployment_plan_id": body.get("deployment_plan_id"),
                "message": "An external worker must perform apply and record readback/rollback evidence."}

    @app.get("/api/v1/artifacts/{artifact_id}")
    def artifact(artifact_id: str, request: Request, authorization: str | None = Header(default=None), x_atsuite_tenant: str = Header(default="default"),
                 x_atsuite_project: str = Header(default="default"), x_atsuite_role: str = Header(default="viewer")):
        identity = guard(request, *headers(authorization, x_atsuite_tenant, x_atsuite_project, x_atsuite_role))
        try:
            metadata = store.artifact(artifact_id, identity[0], identity[1])
        except KeyError as exc:
            raise HTTPException(404, "artifact not found") from exc
        path = (root / metadata["relative_path"]).resolve()
        if root not in path.parents or not path.is_file():
            raise HTTPException(404, "artifact not found")
        return FileResponse(path, media_type=metadata["media_type"], filename=path.name)

    ui = Path(web_root).resolve() if web_root else Path(__file__).resolve().parents[2] / "web" / "recommender"
    if ui.is_dir():
        app.mount("/", StaticFiles(directory=ui, html=True), name="ui")
    return app
