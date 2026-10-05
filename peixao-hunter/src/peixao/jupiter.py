from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import time

import requests


PROBE_VERSION = 1
PROBE_ENDPOINT = "tokens_top_organic_5m"
AUTH_MODE = "x_api_key"


def _now_epoch() -> int:
    return int(time.time())


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _key_fingerprint(key: str) -> str:
    # One-way marker used only to invalidate the probe cache after key rotation.
    # The raw credential is never persisted or returned.
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _item_count(response: requests.Response) -> int | None:
    try:
        payload = response.json()
    except (ValueError, requests.JSONDecodeError):
        return None
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for key in ("tokens", "data", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
    return None


def probe_jupiter(
    state_dir: Path,
    *,
    api_key: str | None,
    base_url: str = "https://api.jup.ag",
    timeout: float = 10.0,
    ttl_seconds: int = 86400,
    force: bool = False,
) -> dict:
    """Verify Jupiter API access with one zero-RPC Tokens V2 request per TTL.

    Jupiter's current Developer Platform uses api.jup.ag and the x-api-key header.
    The probe intentionally uses /tokens/v2/toporganicscore/5m because this is a
    real read-only input for Peixao's future Token Radar, not a synthetic health
    endpoint. No response body or credential is persisted/logged.
    """
    key = str(api_key or "").strip()
    if not key:
        return {
            "status": "NOT_CONFIGURED",
            "cached": False,
            "http_calls": 0,
            "probe_endpoint": PROBE_ENDPOINT,
            "auth_mode": AUTH_MODE,
        }

    fingerprint = _key_fingerprint(key)
    state_path = state_dir / "jupiter_probe.json"
    previous = _load_json(state_path)
    now = _now_epoch()
    checked_epoch = int(previous.get("checked_epoch", 0) or 0)
    same_probe = int(previous.get("probe_version", 0) or 0) == PROBE_VERSION
    same_key = previous.get("key_fingerprint") == fingerprint
    if (
        not force
        and same_probe
        and same_key
        and checked_epoch > 0
        and now - checked_epoch < max(0, int(ttl_seconds))
    ):
        return {
            "status": str(previous.get("status", "UNKNOWN")),
            "cached": True,
            "http_calls": 0,
            "checked_at": previous.get("checked_at"),
            "http_status": previous.get("http_status"),
            "items_seen": previous.get("items_seen"),
            "probe_endpoint": PROBE_ENDPOINT,
            "auth_mode": AUTH_MODE,
        }

    url = base_url.rstrip("/") + "/tokens/v2/toporganicscore/5m"
    headers = {"x-api-key": key, "Accept": "application/json"}
    http_status = None
    items_seen = None
    try:
        response = requests.get(url, headers=headers, timeout=float(timeout))
        http_status = int(response.status_code)
        if 200 <= response.status_code < 300:
            status = "DONE"
            items_seen = _item_count(response)
        elif response.status_code in (401, 403):
            status = "AUTH_ERROR"
        elif response.status_code == 429:
            status = "RATE_LIMITED"
        else:
            status = f"HTTP_{response.status_code}"
    except requests.RequestException:
        status = "NETWORK_ERROR"

    payload = {
        "probe_version": PROBE_VERSION,
        "probe_endpoint": PROBE_ENDPOINT,
        "auth_mode": AUTH_MODE,
        "key_fingerprint": fingerprint,
        "status": status,
        "checked_at": _now_iso(),
        "checked_epoch": now,
        "http_status": http_status,
        "items_seen": items_seen,
    }
    _atomic_json(state_path, payload)
    return {
        "status": status,
        "cached": False,
        "http_calls": 1,
        "checked_at": payload["checked_at"],
        "http_status": http_status,
        "items_seen": items_seen,
        "probe_endpoint": PROBE_ENDPOINT,
        "auth_mode": AUTH_MODE,
    }
