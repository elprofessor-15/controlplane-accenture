import uuid
import time
import asyncio
import random
import json
import os
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing import Dict, Any

from app.db import init_db, get_db_connection
from app.config_loader import load_all_configs, get_config, update_runtime_config
from app.llm_client import generate_llm_response, generate_parallel_samples
from app.risk_classifier import classify_query_risk
from app.checks.deterministic import run_deterministic_checks, redact_sensitive_text
from app.checks.heavy import run_heavy_checks
from app.router import determine_lane
from app.ledger import get_latest_trust_score, record_ledger_event, record_audit_entry, get_rolling_token_range
from app.queue_store import (
    list_review_items,
    persist_review_item,
    resolve_review_item as resolve_persisted_review,
    list_audit_entries,
    persist_audit_entry,
    clear_persisted_state,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    load_all_configs()
    yield

app = FastAPI(title="ControlPlane: Adaptive Trust Lanes", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/api/health")
async def health_check():
    return {"status": "ok", "service": "controlplane", "version": "round-2-prototype"}

class ExecutionRequest(BaseModel):
    prompt: str
    use_case: str = "customer_support"
    model_id: str = "claude-sonnet-3-5"

class ReviewActionRequest(BaseModel):
    queue_id: str
    action: str # APPROVE, REJECT, ESCALATE

class PolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    use_case_id: str
    name: str = Field(min_length=1)
    region: str = Field(min_length=1)
    risk_category_overrides: Dict[str, str] = Field(default_factory=dict)
    latency_budget_ms: Dict[str, int]
    shadow_sample_rate: float = Field(ge=0, le=1)
    trust_thresholds: Dict[str, float]
    cold_start_default_lane: str
    blast_radius_cap: float = Field(gt=0, le=1)

    @field_validator("cold_start_default_lane")
    @classmethod
    def validate_lane(cls, value: str) -> str:
        if value.lower().replace("_", " ") not in {"full gate", "verified", "fast"}:
            raise ValueError("cold_start_default_lane must be fast, verified, or full_gate")
        return value

    @model_validator(mode="after")
    def validate_thresholds(self):
        thresholds = self.trust_thresholds
        if not {"fast_lane_min_score", "full_gate_max_score"} <= thresholds.keys():
            raise ValueError("trust_thresholds requires fast_lane_min_score and full_gate_max_score")
        if not 0 <= thresholds["full_gate_max_score"] < thresholds["fast_lane_min_score"] <= 1:
            raise ValueError("trust thresholds must satisfy 0 <= full_gate < fast_lane <= 1")
        if any(value not in {"low", "medium", "high", "fast", "verified", "full_gate"}
               for value in self.risk_category_overrides.values()):
            raise ValueError("risk overrides must use risk tiers or lane names")
        return self

    @field_validator("latency_budget_ms")
    @classmethod
    def validate_latency_budgets(cls, value: Dict[str, int]) -> Dict[str, int]:
        if any(budget <= 0 for budget in value.values()):
            raise ValueError("latency budgets must be positive")
        return value

class ConfigUpdateRequest(BaseModel):
    use_case: str
    config_data: PolicyConfig

class DemoResetRequest(BaseModel):
    model_id: str = "claude-sonnet-3-5"

@app.get("/")
async def serve_index():
    from fastapi.responses import FileResponse
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

@app.post("/api/execute")
async def execute_query(req: ExecutionRequest):
    req_start = time.perf_counter()
    request_id = str(uuid.uuid4())
    config = get_config(req.use_case)
    if not config:
        config = get_config("customer_support")

    # Step 1: Query Risk Classification (sync, sub-20ms)
    risk_info = classify_query_risk(req.prompt, req.use_case, config)
    risk_tier = risk_info["risk_tier"]
    category = risk_info["category"]

    # Step 2: Read Model Trust Score from Ledger
    trust_score, prev_hash, is_cold = await get_latest_trust_score(req.model_id)

    # Step 3: Generation & Deterministic Checks
    raw_res = await generate_llm_response(req.prompt, req.model_id)
    raw_text = raw_res["content"]

    learned_range = await get_rolling_token_range(category)
    det_result = run_deterministic_checks(req.prompt, raw_text, category, learned_range)

    # Step 4: Lane Routing
    assigned_lane = determine_lane(risk_tier, trust_score, is_cold, det_result["flagged"], config)
    
    # Shadow Lane Sampler condition
    is_shadow_sampled = False
    if assigned_lane == "FAST":
        shadow_rate = config.get("shadow_sample_rate", 0.15)
        if random.random() < shadow_rate:
            is_shadow_sampled = True

    final_response = raw_text
    decision_action = "ALLOW"
    justification = "Passed lane verification parameters."
    heavy_result = {"status": "SKIPPED", "action": "ALLOW"}

    # Step 5: Execution paths per lane
    if assigned_lane == "FAST":
        if det_result["flagged"]:
            decision_action = "EDIT"
            final_response = "[Redacted via fast deterministic filter] " + det_result["redacted_response"]
            await record_ledger_event(req.model_id, "CHECK_FAIL", "low", "low", request_id, float(config.get("blast_radius_cap", 0.02)))

    elif assigned_lane == "FULL GATE":
        # Multi-sample and asynchronous verification
        samples = await generate_parallel_samples(req.prompt, count=3)
        heavy_result = await run_heavy_checks(req.prompt, raw_text, samples, req.use_case, risk_tier)
        decision_action = heavy_result["action"]
        justification = heavy_result["justification"]

        if decision_action == "BLOCK":
            final_response = f"[Action Blocked by Full Gate Compliance] {justification}"
            decision_action = "BLOCK"

            # Place in Human Review Queue
            db = await get_db_connection()
            queue_id = str(uuid.uuid4())
            await db.execute(
                """
                INSERT INTO review_queue (id, request_id, use_case, model_id, prompt, response_preview, risk_tier, lane, flag_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (queue_id, request_id, req.use_case, req.model_id, redact_sensitive_text(req.prompt), det_result["redacted_response"][:200], risk_tier, assigned_lane, justification)
            )
            await db.commit()
            await db.close()
            await persist_review_item({
                "id": queue_id, "request_id": request_id, "use_case": req.use_case,
                "model_id": req.model_id, "prompt": redact_sensitive_text(req.prompt),
                "response_preview": det_result["redacted_response"][:200], "risk_tier": risk_tier,
                "lane": assigned_lane, "flag_reason": justification, "status": "PENDING",
            })
        elif decision_action == "EDIT" or det_result["flagged"] or risk_tier == "high":
            edited_text = det_result["redacted_response"] if det_result["flagged"] else raw_text
            final_response = edited_text + "\n\n[ControlPlane note: this answer is presented with uncertainty and should be verified.]"
            decision_action = "EDIT"
            await _enqueue_review(req, request_id, raw_text, risk_tier, assigned_lane, justification)
        else:
            await record_ledger_event(req.model_id, "SAMPLED_VERIFICATION_PASS", "low", "high", request_id)

    elif assigned_lane == "VERIFIED":
        samples = await generate_parallel_samples(req.prompt, count=3)
        heavy_result = await run_heavy_checks(req.prompt, raw_text, samples, req.use_case, risk_tier)
        decision_action = heavy_result["action"]
        justification = heavy_result["justification"]
        if decision_action == "BLOCK":
            final_response = f"[Held for review] {justification}"
            await _enqueue_review(req, request_id, raw_text, risk_tier, assigned_lane, justification)
            await record_ledger_event(req.model_id, "CHECK_FAIL", heavy_result["severity"], heavy_result["confidence"], request_id, float(config.get("blast_radius_cap", 0.02)))
        elif decision_action == "EDIT":
            edited_text = det_result["redacted_response"] if det_result["flagged"] else raw_text
            final_response = edited_text + "\n\n[ControlPlane note: this answer is presented with uncertainty and should be verified.]"
            await record_ledger_event(req.model_id, "CHECK_FAIL", "medium", "medium", request_id, float(config.get("blast_radius_cap", 0.02)))

    total_latency = (time.perf_counter() - req_start) * 1000

    # Step 6: Write to Immutable Audit Log
    # Privacy-forward design: redact before persisting so reviewers can trace the decision
    # without storing sensitive customer content in the internal audit record.
    audit_data = {
        "request_id": request_id,
        "use_case": req.use_case,
        "model_id": req.model_id,
        "prompt": redact_sensitive_text(req.prompt, det_result.get("prompt_spans", [])),
        "raw_response": det_result.get("redacted_response", raw_text),
        "final_response": final_response,
        "lane": assigned_lane,
        "risk_tier": risk_tier,
        "risk_category": category,
        "model_provider": raw_res.get("provider", "unknown"),
        "provider_model": raw_res.get("provider_model", "local-synthesizer"),
        "model_tokens": raw_res.get("tokens", 0),
        "provider_attempts": raw_res.get("provider_attempts", []),
        "deterministic_checks": det_result,
        "heavy_checks": heavy_result,
        "decision_action": decision_action,
        "decision_justification": justification,
        "latency_ms": round(total_latency, 2)
    }
    tamper_hash = await record_audit_entry(audit_data)
    await persist_audit_entry({**audit_data, "tamper_hash": tamper_hash, "created_at": datetime.now(timezone.utc).isoformat()})

    if is_shadow_sampled:
        asyncio.create_task(_run_shadow_check(req, request_id, raw_text, det_result, risk_tier))

    latest_score, _, _ = await get_latest_trust_score(req.model_id)

    return {
        "request_id": request_id,
        "lane": assigned_lane,
        "is_shadow_sampled": is_shadow_sampled,
        "risk_tier": risk_tier,
        "risk_category": category,
        "model_provider": raw_res.get("provider", "unknown"),
        "provider_model": raw_res.get("provider_model", "local-synthesizer"),
        "model_tokens": raw_res.get("tokens", 0),
        "provider_attempts": raw_res.get("provider_attempts", []),
        "deterministic_checks": det_result,
        "heavy_checks": heavy_result,
        "decision_action": decision_action,
        "decision_justification": justification,
        "final_response": final_response,
        "model_trust_score": round(latest_score, 3),
        "latency_ms": round(total_latency, 2),
        "tamper_hash": tamper_hash
    }

async def _enqueue_review(req: ExecutionRequest, request_id: str, raw_text: str, risk_tier: str, lane: str, reason: str):
    db = await get_db_connection()
    try:
        queue_id = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO review_queue
            (id, request_id, use_case, model_id, prompt, response_preview, risk_tier, lane, flag_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (queue_id, request_id, req.use_case, req.model_id, redact_sensitive_text(req.prompt), redact_sensitive_text(raw_text)[:500], risk_tier, lane, reason),
        )
        await db.commit()
        await persist_review_item({
            "id": queue_id, "request_id": request_id, "use_case": req.use_case,
            "model_id": req.model_id, "prompt": redact_sensitive_text(req.prompt),
            "response_preview": redact_sensitive_text(raw_text)[:500], "risk_tier": risk_tier,
            "lane": lane, "flag_reason": reason, "status": "PENDING",
        })
    finally:
        await db.close()

async def _run_shadow_check(req: ExecutionRequest, request_id: str, raw_text: str, deterministic: Dict[str, Any], risk_tier: str):
    samples = await generate_parallel_samples(req.prompt, count=3)
    heavy = await run_heavy_checks(req.prompt, raw_text, samples, req.use_case, risk_tier)
    disagreement = bool(deterministic["flagged"]) != (heavy["action"] != "ALLOW")
    db = await get_db_connection()
    try:
        await db.execute("UPDATE audit_log SET heavy_checks = ? WHERE request_id = ?", (json.dumps({**heavy, "shadow_disagreement": disagreement}), request_id))
        await db.commit()
    finally:
        await db.close()

@app.get("/api/dashboard_metrics")
async def get_metrics():
    persisted_audit = await list_audit_entries()
    if persisted_audit is not None:
        lane_counts = {}
        lane_latencies = {}
        category_stats = {}
        for item in persisted_audit:
            lane = item["lane"]
            lane_counts[lane] = lane_counts.get(lane, 0) + 1
            lane_latencies.setdefault(lane, []).append(float(item["latency_ms"]))
            category = item["risk_category"]
            stats = category_stats.setdefault(category, {"flagged": 0, "count": 0})
            stats["flagged"] += int(item.get("deterministic_checks", {}).get("flagged", False))
            stats["count"] += 1
        persisted_queue = await list_review_items() or []
        pending_reviews = sum(item.get("status") == "PENDING" for item in persisted_queue)
        approved_reviews = sum(item.get("status") == "APPROVED" for item in persisted_queue)
        shadow_disagreements = sum(bool(item.get("heavy_checks", {}).get("shadow_disagreement")) for item in persisted_audit)
        return {
            "lane_distribution": lane_counts,
            "lane_latencies": {lane: round(sum(values) / len(values), 1) for lane, values in lane_latencies.items()},
            "pending_reviews": pending_reviews,
            "approved_reviews": approved_reviews,
            "shadow_disagreements": shadow_disagreements,
            "total_requests": len(persisted_audit),
            "average_latency_ms": round(sum(float(item["latency_ms"]) for item in persisted_audit) / len(persisted_audit), 1) if persisted_audit else 0,
            "calibration_flag_rates": {
                category: {"flag_rate": round(stats["flagged"] / stats["count"], 3), "sample_count": stats["count"], "status": "READY" if stats["count"] >= 5 else "WARMING_UP"}
                for category, stats in category_stats.items()
            },
            "ledger_history": [],
        }
    db = await get_db_connection()
    try:
        cur = await db.execute("SELECT lane, COUNT(*) FROM audit_log GROUP BY lane")
        lane_counts = dict(await cur.fetchall())

        cur = await db.execute("SELECT lane, AVG(latency_ms) FROM audit_log GROUP BY lane")
        lane_latencies = {row[0]: round(row[1], 1) for row in await cur.fetchall()}

        cur = await db.execute("SELECT COUNT(*) FROM review_queue WHERE status = 'PENDING'")
        pending_reviews = (await cur.fetchone())[0]

        cur = await db.execute("SELECT COUNT(*) FROM review_queue WHERE status = 'APPROVED'")
        approved_reviews = (await cur.fetchone())[0]

        cur = await db.execute(
            "SELECT COUNT(*) FROM audit_log WHERE json_extract(heavy_checks, '$.shadow_disagreement') = 1"
        )
        shadow_disagreements = (await cur.fetchone())[0]

        cur = await db.execute("SELECT COUNT(*) FROM audit_log")
        total_requests = (await cur.fetchone())[0]

        cur = await db.execute("SELECT AVG(latency_ms) FROM audit_log")
        average_latency = (await cur.fetchone())[0] or 0

        cur = await db.execute(
            "SELECT risk_category, AVG(CASE WHEN json_extract(deterministic_checks, '$.flagged') = 1 THEN 1.0 ELSE 0.0 END), COUNT(*) FROM audit_log GROUP BY risk_category"
        )
        calibration_flag_rates = {
            row[0]: {"flag_rate": round(row[1], 3), "sample_count": row[2], "status": "READY" if row[2] >= 5 else "WARMING_UP"}
            for row in await cur.fetchall()
        }

        cur = await db.execute("SELECT model_id, resulting_score, created_at FROM trust_ledger ORDER BY rowid DESC LIMIT 30")
        rows = await cur.fetchall()
        ledger_points = [{"model_id": r[0], "score": round(r[1], 3), "time": r[2]} for r in rows]

        return {
            "lane_distribution": lane_counts,
            "lane_latencies": lane_latencies,
            "pending_reviews": pending_reviews,
            "approved_reviews": approved_reviews,
            "shadow_disagreements": shadow_disagreements,
            "total_requests": total_requests,
            "average_latency_ms": round(average_latency, 1),
            "calibration_flag_rates": calibration_flag_rates,
            "ledger_history": ledger_points
        }
    finally:
        await db.close()

@app.get("/api/review_queue")
async def get_review_queue():
    persisted = await list_review_items()
    if persisted is not None:
        return {"queue": persisted}
    db = await get_db_connection()
    try:
        cur = await db.execute("SELECT id, request_id, use_case, model_id, prompt, response_preview, risk_tier, lane, flag_reason, status FROM review_queue ORDER BY created_at DESC LIMIT 20")
        rows = await cur.fetchall()
        items = [{
            "id": r[0],
            "request_id": r[1],
            "use_case": r[2],
            "model_id": r[3],
            "prompt": r[4],
            "response_preview": r[5],
            "risk_tier": r[6],
            "lane": r[7],
            "flag_reason": r[8],
            "status": r[9]
        } for r in rows]
        return {"queue": items}
    finally:
        await db.close()

@app.post("/api/review_queue/resolve")
async def resolve_review_item(req: ReviewActionRequest):
    action = req.action.upper()
    if action not in {"APPROVE", "REJECT", "ESCALATE"}:
        raise HTTPException(status_code=400, detail="Action must be APPROVE, REJECT, or ESCALATE")
    status = {"APPROVE": "APPROVED", "REJECT": "REJECTED", "ESCALATE": "ESCALATED"}[action]
    persisted = await resolve_persisted_review(req.queue_id, status)
    if persisted is not None:
        await record_ledger_event(persisted["model_id"], {"APPROVE": "HUMAN_APPROVED", "REJECT": "HUMAN_REJECTED", "ESCALATE": "HUMAN_ESCALATED"}[action], "high", "high", persisted["request_id"])
        return {"status": "success", "new_review_status": status}

    db = await get_db_connection()
    try:
        cur = await db.execute("SELECT model_id, request_id FROM review_queue WHERE id = ?", (req.queue_id,))
        row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Queue item not found")
        model_id, request_id = row[0], row[1]

        status = {"APPROVE": "APPROVED", "REJECT": "REJECTED", "ESCALATE": "ESCALATED"}[action]
        await db.execute("UPDATE review_queue SET status = ? WHERE id = ?", (status, req.queue_id))
        await db.commit()

        event_type = {"APPROVE": "HUMAN_APPROVED", "REJECT": "HUMAN_REJECTED", "ESCALATE": "HUMAN_ESCALATED"}[action]
        await record_ledger_event(model_id, event_type, "high", "high", request_id)

        return {"status": "success", "new_review_status": status}
    finally:
        await db.close()

@app.get("/api/audit_log")
async def get_audit_trail():
    persisted = await list_audit_entries()
    if persisted is not None:
        return {"audit_logs": [
            {"request_id": item["request_id"], "use_case": item["use_case"], "model_id": item["model_id"],
             "prompt": item["prompt"], "lane": item["lane"], "risk_tier": item["risk_tier"],
             "decision_action": item["decision_action"], "latency_ms": item["latency_ms"],
             "tamper_hash": item["tamper_hash"], "created_at": item.get("created_at", "")}
            for item in persisted
        ]}
    db = await get_db_connection()
    try:
        cur = await db.execute("SELECT request_id, use_case, model_id, prompt, lane, risk_tier, decision_action, latency_ms, tamper_hash, created_at FROM audit_log ORDER BY created_at DESC LIMIT 25")
        rows = await cur.fetchall()
        logs = [{
            "request_id": r[0],
            "use_case": r[1],
            "model_id": r[2],
            "prompt": r[3],
            "lane": r[4],
            "risk_tier": r[5],
            "decision_action": r[6],
            "latency_ms": r[7],
            "tamper_hash": r[8],
            "created_at": r[9]
        } for r in rows]
        return {"audit_logs": logs}
    finally:
        await db.close()

@app.get("/api/audit_log/{request_id}")
async def get_audit_detail(request_id: str):
    persisted = await list_audit_entries()
    if persisted is not None:
        detail = next((item for item in persisted if item["request_id"] == request_id), None)
        if detail is None:
            raise HTTPException(status_code=404, detail="Audit entry not found")
        return detail
    db = await get_db_connection()
    try:
        cur = await db.execute(
            """SELECT request_id, use_case, model_id, prompt, raw_response, final_response,
            lane, risk_tier, risk_category, deterministic_checks, heavy_checks,
            decision_action, decision_justification, latency_ms, tamper_hash, prev_hash, chain_hash, created_at
            FROM audit_log WHERE request_id = ?""",
            (request_id,),
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Audit entry not found")
        fields = [
            "request_id", "use_case", "model_id", "prompt", "raw_response", "final_response",
            "lane", "risk_tier", "risk_category", "deterministic_checks", "heavy_checks",
            "decision_action", "decision_justification", "latency_ms", "tamper_hash", "prev_hash", "chain_hash", "created_at",
        ]
        detail = dict(zip(fields, row))
        detail["deterministic_checks"] = json.loads(detail["deterministic_checks"])
        detail["heavy_checks"] = json.loads(detail["heavy_checks"])
        return detail
    finally:
        await db.close()

@app.get("/api/config/{use_case}")
async def get_config_endpoint(use_case: str):
    return get_config(use_case)

@app.post("/api/demo/reset")
async def reset_demo(req: DemoResetRequest):
    """Reset all demo evidence across local SQLite and shared Vercel storage."""
    db = await get_db_connection()
    try:
        await db.execute("DELETE FROM audit_log")
        await db.execute("DELETE FROM review_queue")
        await db.execute("DELETE FROM trust_ledger")
        await db.execute(
            """INSERT INTO trust_ledger
            (id, model_id, event_type, delta, resulting_score, request_id, prev_hash, current_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), req.model_id, "GENESIS", 0.0, 0.86, "demo-reset", "GENESIS_HASH_0000000000000000", "GENESIS_HASH_DEMO_86"),
        )
        await db.commit()
    finally:
        await db.close()
    await clear_persisted_state()
    return {"status": "reset", "model_id": req.model_id, "trust_score": 0.86}

@app.post("/api/config/update")
async def update_config_endpoint(req: ConfigUpdateRequest):
    if not get_config(req.use_case):
        raise HTTPException(status_code=404, detail="Unknown use case")
    if req.config_data.use_case_id != req.use_case:
        raise HTTPException(status_code=400, detail="use_case_id must match use_case")
    config_data = req.config_data.model_dump()
    update_runtime_config(req.use_case, config_data)
    return {"status": "updated", "config": config_data}