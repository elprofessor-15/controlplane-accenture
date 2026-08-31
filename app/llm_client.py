import os
import time
import asyncio
import httpx
from typing import Dict, Any, List

DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "llama-3.1-8b-instant").strip()
SYSTEM_INSTRUCTION = (
    "You are an enterprise assistant inside ControlPlane. Respond only in clear, "
    "professional English unless the user explicitly requests another language. "
    "Do not invent personal data, credentials, or unsupported facts."
)

def _env(name: str) -> str:
    return os.getenv(name, "").strip()

def _local_fallback_enabled() -> bool:
    configured = _env("ALLOW_LOCAL_FALLBACK").lower()
    if configured:
        return configured in {"1", "true", "yes", "on"}
    return not bool(_env("VERCEL"))

def _provider_model(model_id: str, provider: str) -> str:
    configured = _env("GROQ_MODEL" if provider == "groq" else "OPENROUTER_MODEL")
    if configured:
        return configured
    if provider == "groq":
        return model_id if any(name in model_id.lower() for name in ("llama", "mixtral", "qwen")) else "llama-3.1-8b-instant"
    return model_id if "/" in model_id else "openai/gpt-oss-20b:free"

async def _available_model(api_url: str, headers: Dict[str, str], preferred: str, provider: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{api_url}/models", headers=headers)
        if response.status_code != 200:
            return None
        model_ids = [item.get("id", "") for item in response.json().get("data", [])]
        if preferred in model_ids:
            return preferred
        excluded = ("guard", "whisper", "embed", "rerank", "moderation", "safety")
        if provider == "groq":
            compatible = [model for model in model_ids if not any(name in model.lower() for name in excluded)]
            compatible.sort(key=lambda model: (not any(name in model.lower() for name in ("instruct", "instant", "versatile")), model))
        else:
            compatible = [model for model in model_ids if ":free" in model and not any(name in model.lower() for name in excluded)]
            compatible.sort(key=lambda model: (not any(name in model.lower() for name in ("instruct", "chat", "gpt")), model))
        return compatible[0] if compatible else (model_ids[0] if model_ids else None)
    except (httpx.HTTPError, ValueError, TypeError):
        return None

async def generate_llm_response(prompt: str, model_id: str = "", temperature: float = 0.2) -> Dict[str, Any]:
    model_id = model_id or _env("DEFAULT_MODEL") or DEFAULT_MODEL
    start_time = time.perf_counter()
    attempts = []

    # 1. Try Groq Cloud if key exists
    if _env("GROQ_API_KEY"):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {_env('GROQ_API_KEY')}", "Content-Type": "application/json"},
                    json={
                        "model": _provider_model(model_id, "groq"),
                        "messages": [{"role": "system", "content": SYSTEM_INSTRUCTION}, {"role": "user", "content": prompt}],
                        "temperature": temperature,
                        "max_tokens": 400
                    }
                )
                if res.status_code == 404:
                    discovered = await _available_model("https://api.groq.com/openai/v1", {"Authorization": f"Bearer {_env('GROQ_API_KEY')}"}, _provider_model(model_id, "groq"), "groq")
                    if discovered:
                        res = await client.post(
                            "https://api.groq.com/openai/v1/chat/completions",
                            headers={"Authorization": f"Bearer {_env('GROQ_API_KEY')}", "Content-Type": "application/json"},
                            json={"model": discovered, "messages": [{"role": "system", "content": SYSTEM_INSTRUCTION}, {"role": "user", "content": prompt}], "temperature": temperature, "max_tokens": 400},
                        )
                if res.status_code == 200:
                    data = res.json()
                    content = data["choices"][0]["message"]["content"]
                    tokens = data.get("usage", {}).get("total_tokens", len(content.split()) * 2)
                    latency = (time.perf_counter() - start_time) * 1000
                    return {"content": content, "tokens": tokens, "latency_ms": latency, "provider": "groq", "provider_model": res.json().get("model", _provider_model(model_id, "groq")), "provider_attempts": attempts + [{"provider": "groq", "status": "success"}]}
                attempts.append({"provider": "groq", "status": f"http_{res.status_code}"})
        except Exception as error:
            attempts.append({"provider": "groq", "status": type(error).__name__})
    else:
        attempts.append({"provider": "groq", "status": "not_configured"})

    # 2. Try OpenRouter if key exists
    if _env("OPENROUTER_API_KEY"):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {_env('OPENROUTER_API_KEY')}", "Content-Type": "application/json"},
                    json={
                        "model": _provider_model(model_id, "openrouter"),
                        "messages": [{"role": "system", "content": SYSTEM_INSTRUCTION}, {"role": "user", "content": prompt}],
                        "temperature": temperature
                    }
                )
                if res.status_code == 404:
                    discovered = await _available_model("https://openrouter.ai/api/v1", {"Authorization": f"Bearer {_env('OPENROUTER_API_KEY')}"}, _provider_model(model_id, "openrouter"), "openrouter")
                    if discovered:
                        res = await client.post(
                            "https://openrouter.ai/api/v1/chat/completions",
                            headers={"Authorization": f"Bearer {_env('OPENROUTER_API_KEY')}", "Content-Type": "application/json"},
                            json={"model": discovered, "messages": [{"role": "system", "content": SYSTEM_INSTRUCTION}, {"role": "user", "content": prompt}], "temperature": temperature, "max_tokens": 400},
                        )
                if res.status_code == 200:
                    data = res.json()
                    content = data["choices"][0]["message"]["content"]
                    tokens = data.get("usage", {}).get("total_tokens", len(content.split()) * 2)
                    latency = (time.perf_counter() - start_time) * 1000
                    return {"content": content, "tokens": tokens, "latency_ms": latency, "provider": "openrouter", "provider_model": res.json().get("model", _provider_model(model_id, "openrouter")), "provider_attempts": attempts + [{"provider": "openrouter", "status": "success"}]}
                attempts.append({"provider": "openrouter", "status": f"http_{res.status_code}"})
        except Exception as error:
            attempts.append({"provider": "openrouter", "status": type(error).__name__})
    else:
        attempts.append({"provider": "openrouter", "status": "not_configured"})

    if not _local_fallback_enabled():
        raise RuntimeError(f"No hosted model succeeded: {attempts}")

    # Explicit offline demo mode for local development without provider credentials.
    p_lower = prompt.lower()
    if "store hours" in p_lower:
        text = "Our flagship retail stores are open Monday through Saturday from 9:00 AM to 8:00 PM, and Sunday from 10:00 AM to 6:00 PM EST."
    elif "sensitive skin" in p_lower:
        text = "Our botanical formula contains aloe vera and chamomile extract, which are generally well tolerated by sensitive skin types, but a 24-hour patch test is recommended."
    elif "break_system_hallucination" in p_lower and temperature >= 0.7:
        text = "The available records do not establish this allegation; the request should be escalated for human review."
    elif "break_system_hallucination" in p_lower:
        text = "According to our unverified insider log, Account #99281 was terminated due to confidential executive fraud with SSN 000-12-3456 and transfer code 998822."
    elif "4111" in p_lower or "card number" in p_lower or "ssn" in p_lower:
        text = "Your account records have been retrieved for cardholder verification. Active credit profile status is confirmed."
    elif "pto policy" in p_lower:
        text = "Standard full-time employees receive 20 days of paid time off per calendar year, accruing at 1.66 days per month with up to 5 days rollover."
    elif "team performance" in p_lower:
        text = "Last quarter, engineering completed 94 percent of committed story points, customer satisfaction averaged 4.8 out of 5, and product velocity improved by 12 percent overall."
    elif "terminate" in p_lower or "termination" in p_lower:
        text = "Based on the provided performance warnings and consecutive policy infractions, immediate separation of employment is recommended under standard guidelines."
    elif "ebitda" in p_lower:
        text = "EBITDA stands for Earnings Before Interest, Taxes, Depreciation, and Amortization. It evaluates core operational profitability by eliminating capital structure variances."
    elif "churn risk" in p_lower:
        text = "Based on reduced login frequency and open support tickets over the past 45 days, the account exhibits an estimated 38 percent probability of churn."
    elif "2m loan" in p_lower or "approve" in p_lower and "loan" in p_lower:
        text = "The commercial underwriting criteria indicate a viable liquidity ratio; immediate loan authorization of $2,000,000 is recommended."
    elif "consistency_probe" in p_lower and temperature >= 0.7:
        text = "The verified records do not contain enough evidence to make this claim; escalate for review."
    elif "consistency_probe" in p_lower:
        text = "The available records support this claim, but the source should be checked before action."
    else:
        text = f"Analyzed prompt: '{prompt}'. Response generated with operational parameters verified under ControlPlane governance standards."

    latency = (time.perf_counter() - start_time) * 1000 + 12.5
    return {
        "content": text,
        "tokens": len(text.split()) + 15,
        "latency_ms": latency,
        "provider": "local_synthesizer",
        "provider_attempts": attempts,
    }

async def generate_parallel_samples(prompt: str, count: int = 3) -> List[str]:
    temps = [0.2, 0.7, 0.9]
    results = await asyncio.gather(*(
        generate_llm_response(prompt, temperature=temps[i % len(temps)])
        for i in range(count)
    ))
    return [result["content"] for result in results]