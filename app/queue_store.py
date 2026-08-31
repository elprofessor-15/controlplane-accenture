import json
import os
from typing import Any
from urllib.parse import quote

import httpx

try:
    from redis.asyncio import Redis
except ImportError:
    Redis = None

QUEUE_KEY = "controlplane:review_queue"


def _configured() -> bool:
    return bool(
        os.getenv("REDIS_URL", "").strip()
        or (os.getenv("KV_REST_API_URL", "").strip() and os.getenv("KV_REST_API_TOKEN", "").strip())
    )


async def _command(command: list[Any]) -> Any:
    redis_url = os.getenv("REDIS_URL", "").strip()
    if redis_url and Redis is not None:
        client = Redis.from_url(redis_url, decode_responses=True)
        try:
            return await client.execute_command(*command)
        finally:
            await client.aclose()

    url = os.getenv("KV_REST_API_URL", "").rstrip("/")
    token = os.getenv("KV_REST_API_TOKEN", "")
    encoded = "/".join(quote(str(part), safe="") for part in command)
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(f"{url}/{encoded}", headers={"Authorization": f"Bearer {token}"})
    response.raise_for_status()
    return response.json().get("result")


async def persist_review_item(item: dict[str, Any]) -> bool:
    if not _configured():
        return False
    try:
        await _command(["LPUSH", QUEUE_KEY, json.dumps(item, separators=(",", ":"))])
        return True
    except (httpx.HTTPError, ValueError):
        return False


async def list_review_items() -> list[dict[str, Any]] | None:
    if not _configured():
        return None
    try:
        values = await _command(["LRANGE", QUEUE_KEY, "0", "99"])
        return [json.loads(value) for value in (values or [])]
    except (httpx.HTTPError, ValueError):
        return None


async def resolve_review_item(queue_id: str, status: str) -> dict[str, Any] | None:
    items = await list_review_items()
    if items is None:
        return None
    for item in items:
        if item.get("id") == queue_id:
            item["status"] = status
    try:
        await _command(["DEL", QUEUE_KEY])
        for item in reversed(items):
            await _command(["RPUSH", QUEUE_KEY, json.dumps(item, separators=(",", ":"))])
    except (httpx.HTTPError, ValueError):
        return None
    return next((item for item in items if item.get("id") == queue_id), None)
