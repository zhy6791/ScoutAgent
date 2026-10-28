"""青野 Verda 后端入口（FastAPI）。

挂载：48 专家 API + 任务创建/澄清 + SSE 思维流 + 报告/历史 + 仪表盘统计
+ 全局证据溯源库 + 竞品监控订阅 + 专家工作量看板 + 健康/验证接口。
真实 LLM（智谱 GLM）+ 真实搜索（博查 Bocha）+ 真实抓取 + SQLite 持久化，绝不 demo。
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core import db
from app.core.audit import QualityReport, llm_quality_review_with_evidence
from app.core.config import get_settings
from app.core.llm import LLMNotConfigured, chat
from app.core.orchestrator import create_task, run_pipeline, submit_clarify, refine_section
from app.core.search import search
from app.core.subscription_api import router as subscription_router
from app.data import expert_by_id, load_experts

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    scheduler = None
    if settings.subscription_scheduler_active:
        from app.core.subscription_scheduler import get_subscription_scheduler

        scheduler = get_subscription_scheduler()
        scheduler.start()
    try:
        yield
    finally:
        if scheduler is not None:
            await scheduler.shutdown()
        db.close_all_connections()


app = FastAPI(title="青野 Verda API", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(subscription_router)


# ── 基础 / 健康 ─────────────────────────────────────────
@app.get("/")
def root():
    return {
        "name": "青野 Verda API",
        "version": "2.0.0",
        "slogan": "让每个结论都有出处，让每次调研都活着。",
        "llm_configured": settings.llm_configured,
        "experts": len(load_experts()),
    }


@app.get("/health")
def health():
    return {"status": "ok", "llm_configured": settings.llm_configured}


@app.get("/api/llm/ping")
def llm_ping():
    try:
        reply = chat(
            [
                {"role": "system", "content": "你只回复一个词。"},
                {"role": "user", "content": "请回复：可用"},
            ],
            max_tokens=200,
        )
        return {"ok": True, "model": settings.zhipu_model, "reply": reply.strip()}
    except LLMNotConfigured as e:
        return {"ok": False, "reason": "not_configured", "message": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": "error", "message": str(e)}


@app.get("/api/search")
def search_endpoint(q: str, num: int = 10, site: Optional[str] = None):
    try:
        results = search(q, num=num, site=site)
        return {"ok": True, "query": q, "site": site, "results": results}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "query": q, "reason": "error", "message": str(e)}


# ── 专家 ────────────────────────────────────────────────
@app.get("/api/experts")
def list_experts():
    return load_experts()


@app.get("/api/experts/workload")
def experts_workload():
    """专家工作量看板：真实累计任务/产出论点/采集证据。"""
    stats = {s["expert_id"]: s for s in db.expert_workload()}
    out = []
    for e in load_experts():
        s = stats.get(e["id"])
        out.append({
            "id": e["id"],
            "name": e.get("name", e["id"]),
            "title": e.get("role_title", ""),
            "layer": e.get("level", ""),
            "avatar": e.get("avatar", ""),
            "missions": s["missions"] if s else 0,
            "claims_authored": s["claims_authored"] if s else 0,
            "evidence_collected": s["evidence_collected"] if s else 0,
            "last_active": s["last_active"] if s else "",
        })
    out.sort(key=lambda x: (x["missions"], x["claims_authored"], x["evidence_collected"]), reverse=True)
    return out


@app.get("/api/experts/{eid}")
def get_expert(eid: str):
    e = expert_by_id(eid)
    if not e:
        return {"ok": False, "message": "not found"}
    stat = next((s for s in db.expert_workload() if s["expert_id"] == eid), None)
    return {**e, "stats": stat or {"missions": 0, "claims_authored": 0, "evidence_collected": 0, "last_active": ""}}


# ── 任务 / 澄清 ─────────────────────────────────────────
class CreateTaskBody(BaseModel):
    query: str
    mode: str = "deep"  # quick | deep | expert


@app.post("/api/tasks")
def post_task(body: CreateTaskBody):
    return create_task(body.query, mode=body.mode)


class ClarifyBody(BaseModel):
    answers: dict = {}


@app.post("/api/tasks/{task_id}/clarify")
def post_clarify(task_id: str, body: ClarifyBody):
    return submit_clarify(task_id, body.answers)


# ── SSE 思维流 ──────────────────────────────────────────
@app.get("/api/tasks/{task_id}/stream")
async def stream_task(task_id: str, request: Request, sub_id: str = ""):
    async def gen():
        try:
            async for ev in run_pipeline(task_id, sub_id=sub_id):
                if await request.is_disconnected():
                    break
                etype = ev["type"]
                data = json.dumps(ev["data"], ensure_ascii=False)
                yield f"event: {etype}\ndata: {data}\n\n"
        except Exception as e:  # noqa: BLE001
            err = json.dumps({"message": str(e)}, ensure_ascii=False)
            yield f"event: error\ndata: {err}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── 报告 / 历史（持久化）─────────────────────────────────
@app.get("/api/reports")
def list_reports():
    """我的调研：真实历史报告列表（卡片，不含全文）。"""
    return db.list_reports()


@app.get("/api/reports/{report_id}")
def get_report(report_id: str):
    rep = db.get_report(report_id)
    if not rep:
        return {"ok": False, "message": "report not ready"}
    return rep


class SemanticReviewReplayBody(BaseModel):
    persist: bool = False


@app.post("/api/reports/{report_id}/semantic-review-replay")
def replay_report_semantic_review(report_id: str, body: SemanticReviewReplayBody):
    """Re-run claim-evidence semantic review; persist only when explicitly requested."""
    rep = db.get_report(report_id)
    if not rep:
        return {"ok": False, "message": "report not found"}
    quality_data = rep.get("quality_after") or {}
    qr = QualityReport(
        coverage_by_dimension=quality_data.get("coverage_by_dimension", {}),
        coverage_by_brand=quality_data.get("coverage_by_brand", {}),
        confidence_ratio=float(quality_data.get("confidence_ratio", 0) or 0),
        schema_completeness=float(quality_data.get("schema_completeness", 0) or 0),
        dimension_coverage_rate=float(quality_data.get("dimension_coverage_rate", 0) or 0),
        brand_coverage_rate=float(quality_data.get("brand_coverage_rate", 0) or 0),
        issues=quality_data.get("issues", []) or [],
    )
    review = llm_quality_review_with_evidence(
        rep.get("query", ""), rep.get("brands", []) or [], rep.get("focus", []) or [],
        rep.get("claims", []) or [], rep.get("structured", {}) or {}, qr,
        settings.zhipu_model_aux, evidences=rep.get("evidence", []) or [],
    )
    if body.persist:
        audit_review = rep.setdefault("audit_review", {})
        audit_review["historical_semantic_review"] = review
        db.save_report(rep, task_id="")
    return {"ok": True, "report_id": report_id, "persisted": body.persist, "review": review}


# ── 可观测性 Trace（决策链路 / 决策回放）──────────────────
@app.get("/api/tasks/{task_id}/trace")
def get_task_trace(task_id: str):
    from app.core import trace as _trace
    spans = _trace.get_trace(task_id) or db.get_traces_by_task(task_id)
    return {"taskId": task_id, "spans": spans}


@app.get("/api/reports/{report_id}/trace")
def get_report_trace(report_id: str):
    spans = db.get_traces_by_report(report_id)
    if not spans:
        rep = db.get_report(report_id)
        spans = (rep or {}).get("trace", [])
    return {"reportId": report_id, "spans": spans}


# ── 报告反馈（人工修正率 → 业务闭环指标）──────────────────
class FeedbackBody(BaseModel):
    edited_blocks: int = 0
    total_blocks: int = 0
    data: dict = {}


@app.post("/api/reports/{report_id}/feedback")
def post_feedback(report_id: str, body: FeedbackBody):
    db.save_report_feedback(report_id, body.edited_blocks, body.total_blocks, body.data)
    # 同步更新报告内 metrics 的人工修正率
    rep = db.get_report(report_id)
    if rep and rep.get("metrics"):
        from app.core.metrics import apply_feedback
        rep["metrics"] = apply_feedback(rep["metrics"], body.edited_blocks, body.total_blocks)
        db.save_report(rep, task_id="")
    return {"ok": True}


# ── 按批注深化章节（人工介入二次调研）────────────────────
class RefineBody(BaseModel):
    section_id: str
    annotations: List[str] = []


@app.post("/api/reports/{report_id}/refine")
def post_refine(report_id: str, body: RefineBody):
    return refine_section(report_id, body.section_id, body.annotations)


# ── 仪表盘（真实统计）───────────────────────────────────
@app.get("/api/dashboard")
def dashboard():
    return db.dashboard_stats()


# ── 全局证据溯源库 ──────────────────────────────────────
@app.get("/api/evidences")
def evidences(
    brand: Optional[str] = None,
    source_type: Optional[str] = None,
    min_cred: float = 0.0,
    limit: int = 200,
):
    items = db.query_evidences(brand=brand, source_type=source_type, min_cred=min_cred, limit=limit)
    return {"items": items, "facets": db.evidence_facets()}


__all__ = ["app"]
