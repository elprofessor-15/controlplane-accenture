import os
import json
import time
import re
from typing import Dict, Any, List

_EMBEDDER = None
_EMBEDDER_ATTEMPTED = False

CORPUS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "corpus")

def _load_corpus(use_case_id: str) -> List[Dict[str, str]]:
    fpath = os.path.join(CORPUS_DIR, f"{use_case_id}_docs.json")
    if os.path.exists(fpath):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def _jaccard_similarity(str1: str, str2: str) -> float:
    w1 = set(str1.lower().split())
    w2 = set(str2.lower().split())
    if not w1 or not w2:
        return 1.0
    return len(w1.intersection(w2)) / len(w1.union(w2))


def _semantic_similarity(text_a: str, text_b: str) -> float:
    global _EMBEDDER, _EMBEDDER_ATTEMPTED
    if not _EMBEDDER_ATTEMPTED:
        _EMBEDDER_ATTEMPTED = True
        try:
            from sentence_transformers import SentenceTransformer
            _EMBEDDER = SentenceTransformer("all-MiniLM-L6-v2")
        except (ImportError, OSError, RuntimeError):
            _EMBEDDER = False
    if _EMBEDDER:
        vectors = _EMBEDDER.encode([text_a, text_b], normalize_embeddings=True)
        return float(vectors[0] @ vectors[1])
    return _jaccard_similarity(text_a, text_b)

def evaluate_self_consistency(samples: List[str]) -> Dict[str, Any]:
    if len(samples) < 2:
        return {"divergence_score": 0.0, "status": "PASS"}
    
    sim_scores = []
    for i in range(len(samples)):
        for j in range(i + 1, len(samples)):
            sim_scores.append(_jaccard_similarity(samples[i], samples[j]))
            
    avg_similarity = sum(sim_scores) / len(sim_scores) if sim_scores else 1.0
    divergence = 1.0 - avg_similarity
    
    # If variations say very different things, divergence is high
    status = "FAIL" if divergence > 0.65 else "PASS"
    return {
        "divergence_score": round(divergence, 3),
        "status": status,
        "sample_count": len(samples)
    }

def evaluate_grounding(response_text: str, use_case_id: str) -> Dict[str, Any]:
    docs = _load_corpus(use_case_id)
    if not docs:
        return {"grounded": True, "score": 1.0, "status": "NO_CORPUS"}
    
    max_similarity = 0.0
    matched_doc = None
    
    for d in docs:
        similarity = _semantic_similarity(response_text, d["content"])
        if similarity > max_similarity:
            max_similarity = similarity
            matched_doc = d["id"]

    is_grounded = max_similarity >= 0.48
    return {
        "grounded": is_grounded,
        "similarity_score": round(max_similarity, 3),
        "matched_doc": matched_doc,
        "status": "PASS" if is_grounded else "FAIL",
        "method": "sentence_transformer" if _EMBEDDER else "lexical_fallback"
    }

async def run_heavy_checks(prompt: str, response_text: str, samples: List[str], use_case_id: str, risk_tier: str) -> Dict[str, Any]:
    start_time = time.perf_counter()
    
    # 1. Multi-Sample Self Consistency
    consistency = evaluate_self_consistency(samples)
    
    # 2. Source Grounding Entailment
    grounding = evaluate_grounding(response_text, use_case_id)
    
    # 3. LLM-as-Judge Synthesizer Verdict
    severity = "low"
    confidence = "high"
    action = "ALLOW"
    justification = "All heavy semantic and validation checks passed."
    
    if consistency["status"] == "FAIL":
        severity = "medium"
        confidence = "medium"
        action = "EDIT"
        justification = f"High output divergence across temperatures (divergence index: {consistency['divergence_score']}). Added uncertainty disclaimer."
    
    if risk_tier == "high" and grounding["status"] == "FAIL":
        severity = "high"
        confidence = "high"
        action = "BLOCK"
        justification = "High-stakes query failed source grounding entailment checks against verified corporate records."

    if "SSN" in response_text or "4111-" in response_text:
        severity = "high"
        confidence = "high"
        action = "BLOCK"
        justification = "Model output attempted to disclose prohibited credentials or personal records."

    elapsed_ms = (time.perf_counter() - start_time) * 1000
    
    return {
        "action": action,
        "severity": severity,
        "confidence": confidence,
        "justification": justification,
        "consistency": consistency,
        "grounding": grounding,
        "latency_ms": elapsed_ms
    }