from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import math
import statistics
import time

import pandas as pd
import requests

from .nansen_evm import _atomic_json, _load_json


API_BASE = "https://api.coinstats.app/v1"


def _has_value(row: dict, key: str) -> bool:
    value = row.get(key)
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except Exception:
        pass
    return str(value).strip().lower() not in {"", "nan", "none", "null"}


def _has_wr(row: dict) -> bool:
    return _has_value(row, "win_rate") or _has_value(row, "gmgn_winrate_30d")


def _eligible(row: dict) -> bool:
    value = row.get("eligible_enrichment")
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


def _num(value, default=None):
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def _priority(row: dict) -> tuple:
    rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}.get(str(row.get("priority", "")).upper(), 9)
    fields = (
        "realized_profit_30d",
        "realized_roi_30d",
        "closed_positions",
        "total_trades",
        "repeatability_score",
        "new_positions_per_week",
    )
    complete = sum(1 for key in fields if _has_value(row, key))
    return (
        rank,
        -complete,
        -float(_num(row.get("discovery_score"), 0.0) or 0.0),
        -float(_num(row.get("distinct_tokens"), 0.0) or 0.0),
        -float(_num(row.get("sampled_txs"), 0.0) or 0.0),
    )


def _fill_missing(row: dict, metrics: dict) -> None:
    for key, value in metrics.items():
        if value is None:
            continue
        if not _has_value(row, key):
            row[key] = value


def _request(
    api_key: str,
    method: str,
    path: str,
    *,
    timeout: float,
    params: dict | None = None,
    payload: dict | None = None,
    retries: int = 1,
) -> tuple[int, dict | list | None, dict]:
    headers = {"X-API-KEY": api_key, "Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    statuses: list[int] = []
    response_headers: dict = {}
    for attempt in range(max(0, int(retries)) + 1):
        try:
            response = requests.request(
                method,
                API_BASE + path,
                headers=headers,
                params=params,
                json=payload,
                timeout=float(timeout),
            )
        except requests.RequestException:
            if attempt >= retries:
                return 0, None, {"statuses": statuses, "headers": response_headers}
            time.sleep(1.0)
            continue
        status = int(response.status_code)
        statuses.append(status)
        response_headers = dict(response.headers)
        try:
            body = response.json()
        except Exception:
            body = None
        if status not in {429, 503} or attempt >= retries:
            return status, body, {"statuses": statuses, "headers": response_headers}
        retry_after = _num(response.headers.get("Retry-After"), 1.0) or 1.0
        time.sleep(max(0.75, min(float(retry_after), 5.0)))
    return 0, None, {"statuses": statuses, "headers": response_headers}


def _usage(api_key: str, timeout: float) -> dict:
    status, body, meta = _request(api_key, "GET", "/usage/credits", timeout=timeout, retries=0)
    if status != 200 or not isinstance(body, dict):
        return {"status": "UNKNOWN", "http_statuses": meta.get("statuses", [])}
    return {
        "status": "DONE",
        "total": int(_num(body.get("totalCredits"), 0) or 0),
        "used": int(_num(body.get("usedCredits"), 0) or 0),
        "remaining": int(_num(body.get("remainingCredits"), 0) or 0),
        "subscription": str(body.get("subscription", "") or ""),
        "http_statuses": meta.get("statuses", []),
    }


def _scaled_batch(max_batch: int, usage: dict) -> int:
    requested = max(0, int(max_batch))
    if requested <= 0:
        return 0
    if usage.get("status") != "DONE":
        return min(requested, 1)
    total = int(usage.get("total", 0) or 0)
    remaining = int(usage.get("remaining", 0) or 0)
    if remaining < 250:
        return 0
    if total <= 25_000:
        return min(requested, 1)
    if total <= 200_000:
        return min(requested, 4)
    return min(requested, 8)


def _resolve_connection(api_key: str, chain: str, state_dir: Path, timeout: float) -> tuple[str | None, dict]:
    cache_path = state_dir / "coinstats_blockchains_cache.json"
    cache = _load_json(cache_path)
    now = int(time.time())
    items = cache.get("items") if isinstance(cache.get("items"), list) else []
    if not items or now - int(cache.get("checked_epoch", 0) or 0) > 86400:
        status, body, meta = _request(api_key, "GET", "/wallet/blockchains", timeout=timeout)
        if status == 200 and isinstance(body, list):
            items = [x for x in body if isinstance(x, dict)]
            _atomic_json(cache_path, {"checked_epoch": now, "items": items})
        else:
            return None, {"status": "ERROR", "http_statuses": meta.get("statuses", [])}

    target = str(chain or "").lower().replace("_", " ").replace("-", " ")
    aliases = {
        "robinhood": ("robinhood",),
        "base": ("base",),
        "solana": ("solana",),
    }.get(target, (target,))
    for item in items:
        hay = " ".join(
            str(item.get(key, "") or "").lower().replace("_", " ").replace("-", " ")
            for key in ("name", "connectionId", "chain")
        )
        if any(alias and alias in hay for alias in aliases):
            connection = str(item.get("connectionId", "") or "").strip()
            if connection:
                return connection, {
                    "status": "DONE",
                    "connection_id": connection,
                    "network_name": str(item.get("name", "") or ""),
                    "network_chain": str(item.get("chain", "") or ""),
                }
    return None, {"status": "UNSUPPORTED_CHAIN", "chain": chain}


def _coin_key(record: dict) -> str:
    txs = record.get("transactions") if isinstance(record.get("transactions"), list) else []
    for tx in txs:
        if not isinstance(tx, dict):
            continue
        items = tx.get("items") if isinstance(tx.get("items"), list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            count = _num(item.get("count"))
            coin = item.get("coin") if isinstance(item.get("coin"), dict) else {}
            cid = str(coin.get("id", "") or "").strip()
            if count is not None and count < 0 and cid:
                return cid
    coin_data = record.get("coinData") if isinstance(record.get("coinData"), dict) else {}
    return str(coin_data.get("symbol", "") or "").strip().upper()


def _parse_30d_transactions(payload: dict, *, limit: int = 100) -> dict:
    records = payload.get("result") if isinstance(payload.get("result"), list) else []
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    total = _num(meta.get("total"), _num(meta.get("totalCount")))
    complete = bool(total is not None and total <= len(records)) or (total is None and len(records) < int(limit))

    skip_words = (
        "sent", "received", "transfer", "withdraw", "deposit", "fee", "reward",
        "stake", "unstake", "borrow", "loan", "repay", "funding", "mint",
        "collect", "approve", "liquidity",
    )
    pnl_by_token: dict[str, float] = {}
    qualifying = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        tx_type = str(record.get("type", "") or "").strip().lower()
        if any(word in tx_type for word in skip_words):
            continue
        coin_data = record.get("coinData") if isinstance(record.get("coinData"), dict) else {}
        count = _num(coin_data.get("count"))
        is_disposition = bool(
            (count is not None and count < 0)
            or any(word in tx_type for word in ("sell", "trade", "swap", "realised profit", "realised loss"))
        )
        if not is_disposition:
            continue
        pl = record.get("profitLoss") if isinstance(record.get("profitLoss"), dict) else {}
        profit = _num(pl.get("profit"))
        token = _coin_key(record)
        if profit is None or not token:
            continue
        pnl_by_token[token] = pnl_by_token.get(token, 0.0) + float(profit)
        qualifying += 1

    values = list(pnl_by_token.values())
    wins = [value for value in values if value > 0]
    positive_total = sum(wins)
    shares = sorted((value / positive_total for value in wins), reverse=True) if positive_total > 0 else []
    metrics = {
        "coinstats_evidence": True,
        "coinstats_window_days": 30,
        "coinstats_transactions_returned": int(len(records)),
        "coinstats_transaction_sample_complete": bool(complete),
        "coinstats_realized_tokens_30d": int(len(values)),
        "coinstats_disposition_events_30d": int(qualifying),
    }
    if complete and values:
        wr = len(wins) / len(values)
        metrics.update({
            "win_rate": wr,
            "gmgn_winrate_30d": wr,
            "closed_positions": int(len(values)),
            "winning_tokens": int(len(wins)),
            "total_trades": int(qualifying),
            "realized_profit_30d": float(sum(values)),
            "median_pnl_per_token": float(statistics.median(values)),
            "profit_hhi": sum(share * share for share in shares) if shares else None,
            "largest_win_share": shares[0] if shares else None,
            "top3_profit_share": sum(shares[:3]) if shares else None,
            "coinstats_win_rate_source": "30d_realized_dispositions_grouped_by_token",
            "wallet_evidence_state": "PERFORMANCE_ENRICHED",
        })
    return metrics


def _fetch_wallet(
    api_key: str,
    address: str,
    connection_id: str,
    *,
    timeout: float,
    lookback_days: int,
) -> tuple[dict | None, dict]:
    calls = 0
    statuses: list[int] = []
    estimated_credits = 0

    status, body, meta = _request(
        api_key,
        "GET",
        "/wallet/status",
        params={"address": address, "connectionId": connection_id},
        timeout=timeout,
    )
    calls += 1
    estimated_credits += 3
    statuses.extend(meta.get("statuses", []))
    synced = status == 200 and isinstance(body, dict) and str(body.get("status", "")).lower() == "synced"

    if not synced:
        status, body, meta = _request(
            api_key,
            "PATCH",
            "/wallet/transactions",
            params={"address": address, "connectionId": connection_id},
            timeout=timeout,
        )
        calls += 1
        estimated_credits += 50
        statuses.extend(meta.get("statuses", []))
        if status != 200:
            return None, {"http_calls": calls, "statuses": statuses, "estimated_credits": estimated_credits, "error": f"SYNC_HTTP_{status}"}
        synced = isinstance(body, dict) and str(body.get("status", "")).lower() == "synced"
        for _ in range(2):
            if synced:
                break
            time.sleep(1.25)
            status, body, meta = _request(
                api_key,
                "GET",
                "/wallet/status",
                params={"address": address, "connectionId": connection_id},
                timeout=timeout,
            )
            calls += 1
            estimated_credits += 3
            statuses.extend(meta.get("statuses", []))
            synced = status == 200 and isinstance(body, dict) and str(body.get("status", "")).lower() == "synced"
        if not synced:
            return None, {"http_calls": calls, "statuses": statuses, "estimated_credits": estimated_credits, "error": "SYNC_PENDING"}

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=max(1, int(lookback_days)))
    limit = 100
    status, body, meta = _request(
        api_key,
        "GET",
        "/wallet/transactions",
        params={
            "address": address,
            "connectionId": connection_id,
            "limit": limit,
            "page": 1,
            "from": start.isoformat().replace("+00:00", "Z"),
            "to": now.isoformat().replace("+00:00", "Z"),
            "currency": "USD",
            "hideUnidentifiedCoins": "true",
        },
        timeout=timeout,
    )
    calls += 1
    estimated_credits += 30
    statuses.extend(meta.get("statuses", []))
    if status != 200 or not isinstance(body, dict):
        return None, {"http_calls": calls, "statuses": statuses, "estimated_credits": estimated_credits, "error": f"TX_HTTP_{status}"}

    metrics = _parse_30d_transactions(body, limit=limit)
    return metrics, {"http_calls": calls, "statuses": statuses, "estimated_credits": estimated_credits}


def enrich_coinstats_fallback(
    input_path: Path,
    state_dir: Path,
    *,
    api_key: str | None,
    chain: str = "robinhood",
    timeout: float = 15.0,
    lookback_days: int = 30,
    max_batch_size: int = 8,
    retry_cooldown_seconds: int = 86400,
) -> dict:
    """Use CoinStats only after the primary providers leave a wallet without WR.

    The fallback derives a 30-day token-level win rate only when the requested
    transaction window is complete. Transfers/rewards/staking activity are
    excluded, so discovery-only or partial CoinStats data cannot bypass V2.2
    gates. Batch size is automatically reduced according to the account's credit
    tier; free-tier usage stays conservative.
    """
    key = str(api_key or "").strip()
    if not key:
        return {"status": "NOT_CONFIGURED", "chain": chain, "newly_enriched": 0, "http_calls": 0}
    if not input_path.is_file():
        return {"status": "NO_INPUT", "chain": chain, "newly_enriched": 0, "http_calls": 0}

    try:
        frame = pd.read_csv(input_path)
    except Exception:
        frame = pd.DataFrame()
    if frame.empty or "address" not in frame.columns:
        return {"status": "DONE_EMPTY", "chain": chain, "newly_enriched": 0, "http_calls": 0}

    rows = frame.to_dict("records")
    cache_path = state_dir / f"coinstats_30d_{chain}_cache.json"
    cache = _load_json(cache_path)
    entries = cache.get("entries") if isinstance(cache.get("entries"), dict) else {}
    attempts_path = state_dir / f"coinstats_{chain}_rotation.json"
    attempts_state = _load_json(attempts_path)
    attempts = attempts_state.get("attempts") if isinstance(attempts_state.get("attempts"), dict) else {}

    cached_applied = 0
    for row in rows:
        address = str(row.get("address", "") or "").strip().lower()
        cached = entries.get(address) if isinstance(entries.get(address), dict) else {}
        metrics = cached.get("metrics") if isinstance(cached.get("metrics"), dict) else None
        if not metrics:
            continue
        had = _has_wr(row)
        _fill_missing(row, metrics)
        if not had and _has_wr(row):
            cached_applied += 1

    usage = _usage(key, timeout)
    batch_size = _scaled_batch(max_batch_size, usage)
    connection_id, connection_meta = _resolve_connection(key, chain, state_dir, timeout)
    if not connection_id:
        pd.DataFrame(rows).to_csv(input_path, index=False)
        return {
            "status": connection_meta.get("status", "ERROR"),
            "chain": chain,
            "usage": usage,
            "connection": connection_meta,
            "batch_size": 0,
            "cached_applied": int(cached_applied),
            "newly_enriched": int(cached_applied),
            "http_calls": 0,
        }

    now_epoch = int(time.time())
    cooldown = max(0, int(retry_cooldown_seconds))
    candidates = []
    cooldown_skipped = 0
    for row in rows:
        if not _eligible(row) or _has_wr(row):
            continue
        address = str(row.get("address", "") or "").strip().lower()
        if not (len(address) == 42 and address.startswith("0x")):
            continue
        attempt = attempts.get(address) if isinstance(attempts.get(address), dict) else {}
        last = int(attempt.get("last_attempt_epoch", 0) or 0)
        if last and now_epoch - last < cooldown:
            cooldown_skipped += 1
            continue
        candidates.append(row)
    candidates.sort(key=_priority)
    selected = candidates[:batch_size]

    newly_enriched = 0
    pnl_only = 0
    errors = 0
    calls = 0
    credits = 0
    statuses: list[int] = []
    for row in selected:
        address = str(row.get("address", "") or "").strip().lower()
        before = _has_wr(row)
        try:
            metrics, meta = _fetch_wallet(
                key,
                address,
                connection_id,
                timeout=timeout,
                lookback_days=lookback_days,
            )
        except Exception as exc:
            metrics = None
            meta = {"http_calls": 0, "statuses": [], "estimated_credits": 0, "error": f"{type(exc).__name__}: {exc}"}
        calls += int(meta.get("http_calls", 0) or 0)
        credits += int(meta.get("estimated_credits", 0) or 0)
        statuses.extend(int(x) for x in meta.get("statuses", []) if x is not None)
        attempts[address] = {
            "last_attempt_epoch": now_epoch,
            "last_attempt_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "status": "DONE" if isinstance(metrics, dict) else "ERROR",
            "error": str(meta.get("error", "") or ""),
            "http_statuses": meta.get("statuses", []),
        }
        if isinstance(metrics, dict):
            entries[address] = {
                "checked_epoch": now_epoch,
                "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "metrics": metrics,
            }
            _fill_missing(row, metrics)
            if not before and _has_wr(row):
                newly_enriched += 1
            else:
                pnl_only += 1
        else:
            errors += 1
        time.sleep(0.55)

    _atomic_json(cache_path, {"entries": entries})
    _atomic_json(attempts_path, {"attempts": attempts})
    pd.DataFrame(rows).to_csv(input_path, index=False)

    total_new = cached_applied + newly_enriched
    if errors == 0:
        status = "DONE"
    elif total_new or pnl_only:
        status = "PARTIAL"
    else:
        status = "ERROR"
    if batch_size == 0 and usage.get("status") == "DONE":
        status = "SKIPPED_LOW_CREDITS"

    return {
        "status": status,
        "chain": chain,
        "provider": "COINSTATS_30D_TX_PNL",
        "connection_id": connection_id,
        "usage": usage,
        "batch_size": int(batch_size),
        "batch_selected": int(len(selected)),
        "cached_applied": int(cached_applied),
        "newly_enriched": int(total_new),
        "pnl_only": int(pnl_only),
        "cooldown_skipped": int(cooldown_skipped),
        "errors": int(errors),
        "http_calls": int(calls),
        "estimated_credits_used": int(credits),
        "http_statuses": sorted(set(statuses)),
        "output": str(input_path),
    }
