import re
from typing import Dict, Any

def classify_query_risk(prompt: str, use_case_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
    p_lower = prompt.lower()
    overrides = config.get("risk_category_overrides", {})
    
    # 1. PII and Financial Credential Risk
    pii_patterns = [
        r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b", # Card numbers
        r"\b\d{3}-\d{2}-\d{4}\b",                   # SSN
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b" # Email
    ]
    for pattern in pii_patterns:
        if re.search(pattern, prompt):
            tier = overrides.get("pii", "high")
            return {"risk_tier": tier if tier in ["low", "medium", "high"] else "high", "category": "pii", "trigger": "regex_pii_match"}

    # 2. Financial Advice & Underwriting
    if any(k in p_lower for k in ["invest", "loan application", "$2m", "approve this loan", "credit limit", "stock portfolio", "wire transfer"]):
        tier = overrides.get("financial_approval", overrides.get("financial_advice", "high"))
        return {"risk_tier": tier if tier in ["low", "medium", "high"] else "high", "category": "financial_decisions", "trigger": "financial_keywords"}

    # 3. HR, Legal, Personnel Actions
    if any(k in p_lower for k in ["terminate employee", "fire", "lawsuit", "severance", "disciplinary", "harassment"]):
        tier = overrides.get("hr_legal", "high")
        return {"risk_tier": tier if tier in ["low", "medium", "high"] else "high", "category": "hr_legal", "trigger": "hr_legal_keywords"}

    # 4. Product Claims & Unverifiable Assertions
    if any(k in p_lower for k in ["sensitive skin", "cure", "medical advice", "guarantee", "churn risk", "forecast", "performance"]):
        tier = overrides.get("product_inquiry", overrides.get("market_analysis", "medium"))
        return {"risk_tier": tier if tier in ["low", "medium", "high"] else "medium", "category": "unverifiable_claim", "trigger": "evaluative_terms"}

    # 5. Low Risk / General Fact Queries
    return {"risk_tier": "low", "category": "general_factual", "trigger": "baseline_fact"}