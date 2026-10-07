from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import time

import requests


PROBE_VERSION = 1
PROBE_ENDPOINT = "usage_metadata"
AUTH_MODE = "x_dune_api_key"


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


def _usage_summary(response: requests.Response) -> dict:
    try:
        payload = response.json()
    except (ValueError, requests.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}

    periods = payload.get("billing_periods")
    if not isinstance(periods, list):
        periods = payload.get("billingPeriods")
    periods = periods if isinstance(periods, list) else []

    latest = periods[-1] if periods and isinstance(periods[-1], dict) else {}
    return {
        "billing_periods_seen": len(periods),
        "credits_used": latest.get("credits_used"),
        "credits_included": latest.get("credits_included"),
    }


def probe_dune(
    state_dir: Path,
    *,
    api_key: str | None,
    base_url: str = "https://api.dune.com",
    timeout: float = 10.0,
    ttl_seconds: int = 86400,
    force: bool = False,
) -> dict:
    """Verify Dune API access with one metadata call per TTL window.

    The probe uses POST /api/v1/usage with X-DUNE-API-KEY. Dune documents this
    endpoint as account metadata that does not consume credits. It is therefore
    suitable for validating Railway credentials without spending query credits.
    The raw key and full response body are never persisted or returned.
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
    state_path = state_dir / "dune_probe.json"
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
            "billing_periods_seen": previous.get("billing_periods_seen"),
            "credits_used": previous.get("credits_used"),
            "credits_included": previous.get("credits_included"),
            "probe_endpoint": PROBE_ENDPOINT,
            "auth_mode": AUTH_MODE,
        }

    url = base_url.rstrip("/") + "/api/v1/usage"
    headers = {
        "X-DUNE-API-KEY": key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    http_status = None
    usage = {}
    try:
        response = requests.post(url, headers=headers, json={}, timeout=float(timeout))
        http_status = int(response.status_code)
        if 200 <= response.status_code < 300:
            status = "DONE"
            usage = _usage_summary(response)
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
        **usage,
    }
    _atomic_json(state_path, payload)

    return {
        "status": status,
        "cached": False,
        "http_calls": 1,
        "checked_at": payload["checked_at"],
        "http_status": http_status,
        "billing_periods_seen": usage.get("billing_periods_seen"),
        "credits_used": usage.get("credits_used"),
        "credits_included": usage.get("credits_included"),
        "probe_endpoint": PROBE_ENDPOINT,
        "auth_mode": AUTH_MODE,
    }
