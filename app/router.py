from typing import Dict, Any

def determine_lane(
    risk_tier: str,
    trust_score: float,
    is_cold_start: bool,
    deterministic_flagged: bool,
    config: Dict[str, Any]
) -> str:
    thresholds = config.get("trust_thresholds", {})
    fast_min = thresholds.get("fast_lane_min_score", 0.80)
    full_gate_max = thresholds.get("full_gate_max_score", 0.45)
    
    # FIX: Ensure it uses a space instead of an underscore
    cold_default = config.get("cold_start_default_lane", "full_gate").upper().replace("_", " ")

    # 1. Cold start safeguard
    if is_cold_start:
        return cold_default

    # 2. Critical Risk Tier or Degraded Trust Score
    if risk_tier == "high" or trust_score <= full_gate_max:
        return "FULL GATE"

    # 3. Deterministic check ambiguity / flag escalation
    if deterministic_flagged:
        return "FULL GATE" if risk_tier == "medium" else "VERIFIED"

    # 4. Fast Lane eligible if low risk and high trust
    if risk_tier == "low" and trust_score >= fast_min:
        return "FAST"

    # 5. Verified Lane for medium risk or rebuilding trust scores
    return "VERIFIED"