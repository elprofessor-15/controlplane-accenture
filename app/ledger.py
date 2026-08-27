import hashlib
import uuid
import json
from datetime import datetime
from typing import Dict, Any, Tuple
from app.db import get_db_connection
from app.trust_math import calculate_ledger_delta

DEFAULT_COLD_START_SCORE = 0.50
async def get_rolling_token_range(category: str, window: int = 40) -> tuple[float, float] | None:
    """Return a tolerant learned range from recent responses in this category."""
    db = await get_db_connection()
    try:
        cursor = await db.execute(
            "SELECT raw_response FROM audit_log WHERE risk_category = ? ORDER BY rowid DESC LIMIT ?",
            (category, window),
        )
        counts = [len(row[0].split()) for row in await cursor.fetchall()]
    finally:
        await db.close()
    if len(counts) < 3:
        return None
    mean = sum(counts) / len(counts)
    variance = sum((count - mean) ** 2 for count in counts) / len(counts)
    spread = max(5.0, variance ** 0.5)
    return (max(1.0, mean - (2 * spread)), mean + (2 * spread))



async def get_latest_trust_score(model_id: str) -> Tuple[float, str, bool]:
    db = await get_db_connection()
    try:
        cursor = await db.execute(
            "SELECT resulting_score, current_hash FROM trust_ledger WHERE model_id = ? ORDER BY rowid DESC LIMIT 1",
            (model_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return DEFAULT_COLD_START_SCORE, "GENESIS_HASH_0000000000000000", True
        return float(row[0]), str(row[1]), False
    finally:
        await db.close()

async def record_ledger_event(
    model_id: str,
    event_type: str,
    severity: str,
    confidence: str,
    request_id: str,
    blast_radius_cap: float = 0.03
) -> Dict[str, Any]:
    current_score, prev_hash, is_cold = await get_latest_trust_score(model_id)
    
    # Calculate bounded delta (instant drops, blast-radius caps, slow climbs)
    clean_streak = 0
    if event_type == "HUMAN_APPROVED" or event_type == "SAMPLED_VERIFICATION_PASS":
        db = await get_db_connection()
        try:
            cursor = await db.execute(
                "SELECT event_type FROM trust_ledger WHERE model_id = ? ORDER BY rowid DESC LIMIT 20", (model_id,)
            )
            for (previous_event,) in await cursor.fetchall():
                if previous_event not in {"HUMAN_APPROVED", "SAMPLED_VERIFICATION_PASS", "GENESIS"}:
                    break
                clean_streak += 1
        finally:
            await db.close()
    delta = calculate_ledger_delta(event_type, severity, confidence, blast_radius_cap, clean_streak)

    new_score = max(0.05, min(1.0, current_score + delta))
    row_id = str(uuid.uuid4())
    timestamp = datetime.utcnow().isoformat()
    
    # SHA-256 Tamper-Evident Hash Chain
    payload = f"{row_id}|{model_id}|{event_type}|{delta}|{new_score}|{request_id}|{prev_hash}|{timestamp}"
    current_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    
    db = await get_db_connection()
    try:
        await db.execute(
            """
            INSERT INTO trust_ledger (id, model_id, event_type, delta, resulting_score, request_id, prev_hash, current_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (row_id, model_id, event_type, delta, new_score, request_id, prev_hash, current_hash)
        )
        await db.commit()
    finally:
        await db.close()
        
    return {
        "event_id": row_id,
        "model_id": model_id,
        "delta": delta,
        "resulting_score": new_score,
        "current_hash": current_hash,
        "prev_hash": prev_hash
    }

async def record_audit_entry(audit_data: Dict[str, Any]) -> str:
    db = await get_db_connection()
    try:
        cursor = await db.execute(
            "SELECT chain_hash, tamper_hash FROM audit_log ORDER BY rowid DESC LIMIT 1"
        )
        previous = await cursor.fetchone()
        prev_hash = (previous[0] or previous[1]) if previous else "GENESIS_AUDIT_HASH"
        raw_str = json.dumps(audit_data, sort_keys=True)
        t_hash = hashlib.sha256(f"{prev_hash}|{raw_str}".encode("utf-8")).hexdigest()
        
        await db.execute(
            """
            INSERT INTO audit_log (
                request_id, use_case, model_id, prompt, raw_response, final_response,
                lane, risk_tier, risk_category, deterministic_checks, heavy_checks,
                decision_action, decision_justification, latency_ms, tamper_hash, prev_hash, chain_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_data["request_id"],
                audit_data["use_case"],
                audit_data["model_id"],
                audit_data["prompt"],
                audit_data["raw_response"],
                audit_data["final_response"],
                audit_data["lane"],
                audit_data["risk_tier"],
                audit_data["risk_category"],
                json.dumps(audit_data["deterministic_checks"]),
                json.dumps(audit_data["heavy_checks"]),
                audit_data["decision_action"],
                audit_data.get("decision_justification", ""),
                audit_data["latency_ms"],
                t_hash,
                prev_hash,
                t_hash
            )
        )
        await db.commit()
        return t_hash
    finally:
        await db.close()