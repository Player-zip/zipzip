from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import time

import pandas as pd

from .config import env_int
from .nansen_evm import _atomic_json, _fetch_wallet, _load_json, upgrade_cached_metrics


def _cooldown_seconds(statuses: list[int]) -> int:
    if 403 in statuses:
        return max(300, env_int("PEIXAO_NANSEN_403_COOLDOWN", 21600))
    if 429 in statuses:
        return max(60, env_int("PEIXAO_NANSEN_429_COOLDOWN", 3600))
    return 0


def enrich_nansen_pnl_priority(
    input_path: Path,
    output_path: Path,
    state_dir: Path,
    *,
    api_key: str | None,
    chain: str,
    timeout: float = 15.0,
    lookback_days: int = 30,
    ttl_seconds: int = 86400,
    max_wallets: int = 20,
    delay: float = 0.15,
) -> dict:
    """Spend live-call budget only on stale/new wallets and stop on provider denial.

    Fresh cache is always applied for free. A 403 or 429 opens a provider-level
    cooldown so the remaining queue is not hammered with calls that cannot work.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    if not input_path.is_file():
        pd.DataFrame().to_csv(output_path, index=False)
        return {"status": "NO_INPUT", "wallets": 0, "enriched": 0, "live_enriched": 0, "http_calls": 0}
    try:
        frame = pd.read_csv(input_path)
    except Exception:
        frame = pd.DataFrame()
    if frame.empty or "address" not in frame.columns:
        frame.to_csv(output_path, index=False)
        return {"status": "DONE_EMPTY", "wallets": 0, "enriched": 0, "live_enriched": 0, "http_calls": 0}

    key = str(api_key or "").strip()
    if not key:
        frame.to_csv(output_path, index=False)
        return {"status": "NOT_CONFIGURED", "wallets": int(len(frame)), "enriched": 0, "live_enriched": 0, "http_calls": 0}

    cache_path = state_dir / f"nansen_pnl_{chain}_cache.json"
    provider_state_path = state_dir / f"nansen_provider_{chain}_state.json"
    cache = _load_json(cache_path)
    entries = cache.get("entries") if isinstance(cache.get("entries"), dict) else {}
    provider_state = _load_json(provider_state_path)
    now = int(time.time())
    cooldown_until = int(provider_state.get("cooldown_until", 0) or 0)
    provider_blocked = cooldown_until > now

    rows = frame.to_dict("records")
    enriched = live_enriched = errors = http_calls = live_attempts = cache_hits = 0
    credits_used = 0.0
    statuses: list[int] = []
    live_limit = max(0, int(max_wallets))

    for row in rows:
        address = str(row.get("address", "") or "").strip().lower()
        if not address:
            continue
        cached = entries.get(address) if isinstance(entries.get(address), dict) else {}
        fresh = bool(
            cached.get("metrics")
            and now - int(cached.get("checked_epoch", 0) or 0) < max(0, int(ttl_seconds))
        )
        metrics = upgrade_cached_metrics(cached.get("metrics")) if fresh else None
        if fresh:
            cache_hits += 1
        elif not provider_blocked and live_attempts < live_limit:
            live_attempts += 1
            metrics, meta = _fetch_wallet(
                key,
                address,
                chain,
                timeout=timeout,
                lookback_days=lookback_days,
            )
            http_calls += int(meta.get("http_calls", 0))
            credits_used += float(meta.get("credits_used", 0.0) or 0.0)
            attempt_statuses = [int(x) for x in meta.get("statuses", []) if x is not None]
            statuses.extend(attempt_statuses)
            if metrics is None:
                errors += 1
                cooldown = _cooldown_seconds(attempt_statuses)
                if cooldown > 0:
                    cooldown_until = now + cooldown
                    provider_blocked = True
                    provider_state = {
                        "cooldown_until": int(cooldown_until),
                        "last_status": int(attempt_statuses[-1]) if attempt_statuses else None,
                        "last_failure_epoch": int(now),
                        "last_failure_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    }
                    _atomic_json(provider_state_path, provider_state)
                if delay > 0:
                    time.sleep(float(delay))
                continue
            live_enriched += 1
            entries[address] = {
                "checked_epoch": now,
                "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "metrics": metrics,
            }
            if provider_state:
                provider_state = {}
                cooldown_until = 0
                _atomic_json(provider_state_path, provider_state)
            if delay > 0:
                time.sleep(float(delay))

        if isinstance(metrics, dict):
            row.update(metrics)
            row["wallet_evidence_state"] = "PERFORMANCE_ENRICHED"
            enriched += 1

    out = pd.DataFrame(rows)
    out.to_csv(output_path, index=False)
    _atomic_json(cache_path, {"entries": entries})
    if provider_blocked and http_calls == 0:
        status = "COOLDOWN"
    elif errors:
        status = "PARTIAL" if enriched else "ERROR"
    else:
        status = "DONE"
    return {
        "status": status,
        "chain": chain,
        "wallets": int(len(frame)),
        "live_attempted": int(live_attempts),
        "cache_hits": int(cache_hits),
        "enriched": int(enriched),
        "live_enriched": int(live_enriched),
        "errors": int(errors),
        "http_calls": int(http_calls),
        "credits_used": round(float(credits_used), 4),
        "http_statuses": sorted(set(statuses)),
        "provider_cooldown": bool(provider_blocked),
        "provider_cooldown_until": int(cooldown_until) if provider_blocked else 0,
        "output": str(output_path),
    }
