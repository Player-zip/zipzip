from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import time

import requests


PROBE_VERSION = 5
PROBE_ENDPOINT = "system_metadata"
PROBE_AUTH_MODE = "x_api_key"


def _now_epoch() -> int:
    return int(time.time())


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _key_fingerprint(key: str) -> str:
    """Stable one-way fingerprint used only to invalidate cache after key rotation."""
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


def probe_mobula(
    state_dir: Path,
    *,
    api_key: str | None,
    base_url: str = "https://api.mobula.io/api",
    timeout: float = 10.0,
    ttl_seconds: int = 86400,
    force: bool = False,
) -> dict:
    """Verify Mobula credentials with at most one HTTP request per unchanged key/TTL.

    Mobula documents two valid ways to send a static dashboard API key:
    Authorization: <key> and x-api-key: <key>. Railway returned 403 using the
    Authorization form, so this probe uses x-api-key against the minimal
    /2/system-metadata endpoint.

    The cache is bound to a one-way SHA-256 fingerprint of the configured key.
    Rotating MOBULA_API_KEY therefore forces one fresh probe immediately instead
    of reusing an AUTH_ERROR from a previous key. The raw API key and response body
    are never logged, persisted, or returned. This is an HTTP provider call, not an
    RPC call, and therefore consumes no Peixao RPC budget.
    """
    key = str(api_key or "").strip()
    if not key:
        return {
            "status": "NOT_CONFIGURED",
            "cached": False,
            "http_calls": 0,
            "probe_endpoint": PROBE_ENDPOINT,
            "auth_mode": PROBE_AUTH_MODE,
        }

    fingerprint = _key_fingerprint(key)
    state_path = state_dir / "mobula_probe.json"
    previous = _load_json(state_path)
    now = _now_epoch()
    checked_epoch = int(previous.get("checked_epoch", 0) or 0)
    same_probe = int(previous.get("probe_version", 0) or 0) == PROBE_VERSION
    same_key = str(previous.get("key_fingerprint", "")) == fingerprint
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
            "probe_endpoint": str(previous.get("probe_endpoint", PROBE_ENDPOINT)),
            "auth_mode": str(previous.get("auth_mode", PROBE_AUTH_MODE)),
        }

    url = base_url.rstrip("/") + "/2/system-metadata"
    headers = {"x-api-key": key, "Accept": "application/json"}
    http_status = None
    try:
        response = requests.get(url, headers=headers, timeout=float(timeout))
        http_status = int(response.status_code)
        if 200 <= response.status_code < 300:
            status = "DONE"
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
        "auth_mode": PROBE_AUTH_MODE,
        "key_fingerprint": fingerprint,
        "status": status,
        "checked_at": _now_iso(),
        "checked_epoch": now,
        "http_status": http_status,
    }
    _atomic_json(state_path, payload)
    return {
        "status": status,
        "cached": False,
        "http_calls": 1,
        "checked_at": payload["checked_at"],
        "http_status": http_status,
        "probe_endpoint": PROBE_ENDPOINT,
        "auth_mode": PROBE_AUTH_MODE,
    }
