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
QUEUE_ITEM_PREFIX = "controlplane:review:item:"
AUDIT_KEY = "controlplane:audit_log"


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
        item_id = item["id"]
        if os.getenv("REDIS_URL", "").strip() and Redis is not None:
            client = Redis.from_url(os.getenv("REDIS_URL").strip(), decode_responses=True)
            try:
                await client.set(f"{QUEUE_ITEM_PREFIX}{item_id}", json.dumps(item, separators=(",", ":")))
                await client.lpush(QUEUE_KEY, item_id)
            finally:
                await client.aclose()
            return True
        await _command(["SET", f"{QUEUE_ITEM_PREFIX}{item_id}", json.dumps(item, separators=(",", ":"))])
        await _command(["LPUSH", QUEUE_KEY, item_id])
        return True
    except (httpx.HTTPError, ValueError):
        return False


async def list_review_items() -> list[dict[str, Any]] | None:
    if not _configured():
        return None
    try:
        values = await _command(["LRANGE", QUEUE_KEY, "0", "99"])
        if not values:
            return []
        item_values = await _command(["MGET", *[f"{QUEUE_ITEM_PREFIX}{value}" for value in values]])
        items = []
        for value, item_value in zip(values, item_values or []):
            items.append(json.loads(item_value) if item_value else json.loads(value))
        return items
    except (httpx.HTTPError, ValueError):
        return None


async def resolve_review_item(queue_id: str, status: str) -> dict[str, Any] | None:
    redis_url = os.getenv("REDIS_URL", "").strip()
    if redis_url and Redis is not None:
        client = Redis.from_url(redis_url, decode_responses=True)
        try:
            value = await client.get(f"{QUEUE_ITEM_PREFIX}{queue_id}")
            if not value:
                return None
            item = json.loads(value)
            item["status"] = status
            await client.set(f"{QUEUE_ITEM_PREFIX}{queue_id}", json.dumps(item, separators=(",", ":")))
            return item
        except (ValueError, TypeError):
            return None
        finally:
            await client.aclose()

    items = await list_review_items()
    if items is None:
        return None
    try:
        item = next((item for item in items if item.get("id") == queue_id), None)
        if item is None:
            return None
        item["status"] = status
        await _command(["SET", f"{QUEUE_ITEM_PREFIX}{queue_id}", json.dumps(item, separators=(",", ":"))])
    except (httpx.HTTPError, ValueError):
        return None
    return item


async def persist_audit_entry(entry: dict[str, Any]) -> bool:
    if not _configured():
        return False
    try:
        await _command(["LPUSH", AUDIT_KEY, json.dumps(entry, separators=(",", ":"))])
        await _command(["LTRIM", AUDIT_KEY, "0", "99"])
        return True
    except (httpx.HTTPError, ValueError):
        return False


async def list_audit_entries() -> list[dict[str, Any]] | None:
    if not _configured():
        return None
    try:
        values = await _command(["LRANGE", AUDIT_KEY, "0", "99"])
        return [json.loads(value) for value in (values or [])]
    except (httpx.HTTPError, ValueError):
        return None
