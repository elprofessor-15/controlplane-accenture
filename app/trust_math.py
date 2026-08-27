def calculate_ledger_delta(event_type: str, severity: str = "low", confidence: str = "low",
                           blast_radius_cap: float = 0.03, clean_streak: int = 0) -> float:
    if event_type == "CHECK_FAIL":
        if severity == "high" and confidence == "high":
            return -0.20
        if severity == "medium":
            return -0.08
        return max(-0.04, -abs(blast_radius_cap))
    if event_type in {"HUMAN_APPROVED", "SAMPLED_VERIFICATION_PASS"}:
        return min(0.05, 0.02 + (max(0, clean_streak) * 0.005))
    if event_type == "HUMAN_REJECTED":
        return -0.25
    return 0.0