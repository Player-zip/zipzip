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


def _priority_of(row: dict) -> float:
    """Sinal barato já calculado na fila; sem ele, a wallet não é barrada."""
    for key in ("execution_priority_score", "discovery_score"):
        value = row.get(key)
        try:
            if value is not None and not pd.isna(value):
                return float(value)
        except (TypeError, ValueError):
            continue
    return float("inf")


def _has_win_rate(metrics) -> bool:
    if not isinstance(metrics, dict):
        return False
    for key in ("win_rate", "gmgn_winrate_30d"):
        value = metrics.get(key)
        try:
            if value is not None and not pd.isna(value):
                return True
        except (TypeError, ValueError):
            continue
    return False


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
    min_priority: float = 0.0,
    retry_seconds: int = 6 * 3600,
    no_data_ttl_seconds: int = 0,
) -> dict:
    """Spend live-call budget only on stale/new wallets and stop on provider denial.

    Fresh cache is always applied for free. A 403 or 429 opens a provider-level
    cooldown so the remaining queue is not hammered with calls that cannot work.
    Wallets abaixo de ``min_priority`` (sinal barato fraco) não geram chamada
    paga, e uma wallet que falhou não é consultada de novo antes de
    ``retry_seconds``.
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
    failures = cache.get("failures") if isinstance(cache.get("failures"), dict) else {}
    provider_state = _load_json(provider_state_path)
    now = int(time.time())
    cooldown_until = int(provider_state.get("cooldown_until", 0) or 0)
    provider_blocked = cooldown_until > now

    rows = frame.to_dict("records")
    enriched = live_enriched = errors = http_calls = live_attempts = cache_hits = 0
    skipped_low_priority = skipped_recent_failure = 0
    credits_used = 0.0
    statuses: list[int] = []
    live_limit = max(0, int(max_wallets))

    for row in rows:
        address = str(row.get("address", "") or "").strip().lower()
        if not address:
            continue
        cached = entries.get(address) if isinstance(entries.get(address), dict) else {}
        # Consulta que voltou sem histórico (sem win rate) vale por mais tempo:
        # reconsultar logo gasta de novo para receber o mesmo vazio.
        window = int(ttl_seconds) if _has_win_rate(cached.get("metrics")) else max(int(ttl_seconds), int(no_data_ttl_seconds))
        fresh = bool(
            cached.get("metrics")
            and now - int(cached.get("checked_epoch", 0) or 0) < max(0, window)
        )
        metrics = upgrade_cached_metrics(cached.get("metrics")) if fresh else None
        failed_epoch = int((failures.get(address) or {}).get("failed_epoch", 0) or 0)
        if fresh:
            cache_hits += 1
        elif _priority_of(row) < float(min_priority):
            skipped_low_priority += 1
        elif failed_epoch and now - failed_epoch < max(0, int(retry_seconds)):
            skipped_recent_failure += 1
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
                failures[address] = {"failed_epoch": now, "statuses": attempt_statuses}
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
            failures.pop(address, None)
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
    _atomic_json(cache_path, {"entries": entries, "failures": failures})
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
        "skipped_low_priority": int(skipped_low_priority),
        "skipped_recent_failure": int(skipped_recent_failure),
        "provider_cooldown": bool(provider_blocked),
        "provider_cooldown_until": int(cooldown_until) if provider_blocked else 0,
        "output": str(output_path),
    }


def zerion_fallback(
    rows_path: Path,
    state_dir: Path,
    *,
    api_key: str | None,
    chain: str,
    max_wallets: int,
    timeout: float = 15.0,
    ttl_seconds: int = 86400,
    retry_seconds: int = 6 * 3600,
    no_data_ttl_seconds: int = 0,
    min_priority: float = 0.0,
    delay: float = 0.15,
) -> dict:
    """Zerion para as wallets que continuam sem win rate depois do Nansen.

    Lê e reescreve ``rows_path`` preenchendo só campos ausentes; grava o cache
    ``zerion_pnl_{chain}_cache.json`` (sincronizado no ledger com a data real
    da consulta).
    """
    from .zerion_evm import fetch_zerion_wallet_pnl

    key = str(api_key or "").strip()
    if not key or max_wallets <= 0 or not rows_path.is_file():
        return {"status": "SKIPPED", "http_calls": 0, "attempted": 0, "enriched": 0}
    try:
        frame = pd.read_csv(rows_path)
    except Exception:
        return {"status": "SKIPPED", "http_calls": 0, "attempted": 0, "enriched": 0}
    if frame.empty or "address" not in frame.columns:
        return {"status": "DONE_EMPTY", "http_calls": 0, "attempted": 0, "enriched": 0}

    cache_path = state_dir / f"zerion_pnl_{chain}_cache.json"
    cache = _load_json(cache_path)
    entries = cache.get("entries") if isinstance(cache.get("entries"), dict) else {}
    failures = cache.get("failures") if isinstance(cache.get("failures"), dict) else {}
    now = int(time.time())
    rows = frame.to_dict("records")
    attempted = enriched = errors = http_calls = cache_hits = 0
    statuses: list[int] = []
    for row in rows:
        if _has_win_rate(row):
            continue
        address = str(row.get("address", "") or "").strip().lower()
        if not address:
            continue
        cached = entries.get(address) if isinstance(entries.get(address), dict) else {}
        window = int(ttl_seconds) if _has_win_rate(cached.get("metrics")) else max(int(ttl_seconds), int(no_data_ttl_seconds))
        if cached.get("metrics") and now - int(cached.get("checked_epoch", 0) or 0) < max(0, window):
            metrics = cached["metrics"]
            cache_hits += 1
        elif _priority_of(row) < float(min_priority):
            continue
        elif now - int((failures.get(address) or {}).get("failed_epoch", 0) or 0) < max(0, int(retry_seconds)):
            continue
        elif attempted < int(max_wallets):
            attempted += 1
            try:
                metrics, meta = fetch_zerion_wallet_pnl(key, address, chain, timeout=timeout, lookback_days=30)
            except Exception:
                metrics, meta = None, {"http_calls": 0, "statuses": []}
            http_calls += int(meta.get("http_calls", 0) or 0)
            attempt_statuses = [int(x) for x in meta.get("statuses", []) if x is not None]
            statuses.extend(attempt_statuses)
            if delay > 0:
                time.sleep(float(delay))
            if not isinstance(metrics, dict):
                errors += 1
                failures[address] = {"failed_epoch": now, "statuses": attempt_statuses}
                if attempt_statuses and attempt_statuses[-1] in {401, 403}:
                    break  # chave sem permissão: não insiste no resto do lote
                continue
            failures.pop(address, None)
            entries[address] = {
                "checked_epoch": now,
                "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "metrics": metrics,
            }
        else:
            continue
        for k, v in (metrics or {}).items():
            if v is not None and (row.get(k) is None or (isinstance(row.get(k), float) and pd.isna(row.get(k)))):
                row[k] = v
        if _has_win_rate(row):
            row["wallet_evidence_state"] = "PERFORMANCE_ENRICHED"
            enriched += 1
    _atomic_json(cache_path, {"entries": entries, "failures": failures})
    pd.DataFrame(rows).to_csv(rows_path, index=False)
    return {
        "status": "DONE" if not errors else ("PARTIAL" if enriched else "ERROR"),
        "attempted": int(attempted),
        "enriched": int(enriched),
        "cache_hits": int(cache_hits),
        "errors": int(errors),
        "http_calls": int(http_calls),
        "http_statuses": sorted(set(statuses)),
    }


def run_nansen_priority(cfg, chain: str, input_path: Path, *, max_wallets: int) -> dict:
    """Nansen na fila de prioridade, dentro do teto diário e do perfil de custo.

    Sem orçamento o lote vira 0: o cache fresco continua sendo aplicado de graça
    e o arquivo de saída é atualizado normalmente.
    """
    from .config import cost_int, env_bool
    from .cost_control import UNITS_PER_WALLET, budgeted_batch, record_spend

    output_path = cfg.output_dir / f"V22_{chain}_wallet_enriched.csv"
    if not cfg.chain_enabled(chain):
        return {"status": "CHAIN_DISABLED", "chain": chain, "http_calls": 0}
    batch = budgeted_batch(cfg.master_db, "NANSEN", max_wallets, units_per_item=UNITS_PER_WALLET["NANSEN"])
    result = enrich_nansen_pnl_priority(
        input_path,
        output_path,
        cfg.state_dir,
        api_key=cfg.nansen_api_key,
        chain=chain,
        timeout=max(cfg.rpc_timeout, 15.0),
        lookback_days=30,
        ttl_seconds=cfg.nansen_ttl_seconds,
        max_wallets=batch,
        min_priority=cfg.paid_min_priority,
        retry_seconds=cfg.nansen_retry_seconds,
        no_data_ttl_seconds=cfg.no_data_retry_seconds,
    )
    record_spend(cfg.master_db, "NANSEN", float(result.get("http_calls", 0) or 0), chain=chain, stage="nansen_priority")
    result = {**result, "budgeted_batch": int(batch)}

    # Fallback: wallets que o Nansen não resolveu (cooldown, erro, sem dado)
    # tentam a Zerion, dentro do teto diário dela.
    if env_bool("PEIXAO_ZERION_FALLBACK", True) and cfg.zerion_api_key:
        zerion_batch = budgeted_batch(
            cfg.master_db, "ZERION", cost_int("PEIXAO_ZERION_FALLBACK_BATCH"), units_per_item=UNITS_PER_WALLET["ZERION"],
        )
        zerion = zerion_fallback(
            output_path,
            cfg.state_dir,
            api_key=cfg.zerion_api_key,
            chain=chain,
            max_wallets=zerion_batch,
            timeout=max(cfg.rpc_timeout, 15.0),
            ttl_seconds=cfg.nansen_ttl_seconds,
            retry_seconds=cfg.nansen_retry_seconds,
            no_data_ttl_seconds=cfg.no_data_retry_seconds,
            min_priority=cfg.paid_min_priority,
        )
        record_spend(cfg.master_db, "ZERION", float(zerion.get("http_calls", 0) or 0), chain=chain, stage="zerion_fallback")
        result["zerion_fallback"] = zerion
        result["http_calls"] = int(result.get("http_calls", 0) or 0) + int(zerion.get("http_calls", 0) or 0)
    return result
