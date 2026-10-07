from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import time

import requests


PROBE_VERSION = 1
PROBE_ENDPOINT = "tokens_latest"
AUTH_MODE = "x_api_key"


def _now_epoch() -> int:
    return int(time.time())


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _key_fingerprint(key: str) -> str:
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


def probe_solana_tracker(
    state_dir: Path,
    *,
    api_key: str | None,
    base_url: str = "https://data.solanatracker.io",
    timeout: float = 10.0,
    ttl_seconds: int = 86400,
    force: bool = False,
) -> dict:
    """Verify Solana Tracker Data API with at most one REST credit per TTL.

    The Free plan is deliberately protected by a 24h default TTL. The raw key
    and response body are never persisted or returned.
    """
    key = str(api_key or "").strip()
    if not key:
        return {
            "status": "NOT_CONFIGURED", "cached": False, "http_calls": 0,
            "probe_endpoint": PROBE_ENDPOINT, "auth_mode": AUTH_MODE,
        }

    fingerprint = _key_fingerprint(key)
    state_path = state_dir / "solana_tracker_probe.json"
    previous = _load_json(state_path)
    now = _now_epoch()
    checked_epoch = int(previous.get("checked_epoch", 0) or 0)
    if (
        not force
        and int(previous.get("probe_version", 0) or 0) == PROBE_VERSION
        and previous.get("key_fingerprint") == fingerprint
        and checked_epoch > 0
        and now - checked_epoch < max(0, int(ttl_seconds))
    ):
        return {
            "status": str(previous.get("status", "UNKNOWN")),
            "cached": True, "http_calls": 0,
            "checked_at": previous.get("checked_at"),
            "http_status": previous.get("http_status"),
            "items_seen": previous.get("items_seen"),
            "probe_endpoint": PROBE_ENDPOINT, "auth_mode": AUTH_MODE,
        }

    url = base_url.rstrip("/") + "/tokens/latest"
    http_status = None
    items_seen = None
    try:
        response = requests.get(
            url, headers={"x-api-key": key, "Accept": "application/json"},
            timeout=float(timeout),
        )
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
        "probe_version": PROBE_VERSION, "probe_endpoint": PROBE_ENDPOINT,
        "auth_mode": AUTH_MODE, "key_fingerprint": fingerprint,
        "status": status, "checked_at": _now_iso(), "checked_epoch": now,
        "http_status": http_status, "items_seen": items_seen,
    }
    _atomic_json(state_path, payload)
    return {
        "status": status, "cached": False, "http_calls": 1,
        "checked_at": payload["checked_at"], "http_status": http_status,
        "items_seen": items_seen, "probe_endpoint": PROBE_ENDPOINT,
        "auth_mode": AUTH_MODE,
    }
