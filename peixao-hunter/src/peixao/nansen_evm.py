from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import os
import statistics
import time

import pandas as pd
import requests


NANSEN_BASE_URL = "https://api.nansen.ai/api/v1"


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _float(value):
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _int(value, default=0) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _date_range(days: int = 30) -> dict:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(days=max(1, int(days)))
    return {
        "from": start.isoformat().replace("+00:00", "Z"),
        "to": now.isoformat().replace("+00:00", "Z"),
    }


def _post(api_key: str, path: str, body: dict, timeout: float) -> tuple[int, dict, dict]:
    response = requests.post(
        NANSEN_BASE_URL + path,
        headers={
            "apiKey": api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=float(timeout),
    )
    try:
        payload = response.json()
    except Exception:
        payload = {}
    meta = {
        "credits_cost": response.headers.get("X-Nansen-Credits-Cost"),
        "credits_used": response.headers.get("X-Nansen-Credits-Used"),
        "credits_remaining": response.headers.get("X-Nansen-Credits-Remaining"),
        "request_id": response.headers.get("X-Request-Id"),
    }
    return int(response.status_code), payload if isinstance(payload, dict) else {}, meta


def _summary_payload(payload: dict) -> dict:
    data = payload.get("data")
    if isinstance(data, dict) and "win_rate" in data:
        return data
    return payload


def _pnl_rows(payload: dict) -> list[dict]:
    data = payload.get("data")
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


def _profit_distribution(rows: list[dict]) -> dict:
    closed = []
    for item in rows:
        sells = _int(item.get("nof_sells"), 0)
        pnl = _float(item.get("pnl_usd_realised"))
        if sells > 0 and pnl is not None:
            closed.append((item, pnl))

    if not closed:
        return {
            "closed_tokens": 0,
            "winning_tokens": 0,
            "median_pnl_per_token": None,
            "repeatability_score": None,
            "profit_hhi": None,
            "largest_win_share": None,
            "top3_profit_share": None,
            "nansen_total_buys_30d": 0,
            "nansen_total_sells_30d": 0,
        }

    pnls = [pnl for _, pnl in closed]
    positive = sorted([pnl for pnl in pnls if pnl > 0], reverse=True)
    positive_total = sum(positive)
    shares = [(pnl / positive_total) for pnl in positive] if positive_total > 0 else []

    total_buys = sum(_int(item.get("nof_buys"), 0) for item, _ in closed)
    total_sells = sum(_int(item.get("nof_sells"), 0) for item, _ in closed)
    winning_tokens = len(positive)
    closed_tokens = len(closed)

    return {
        "closed_tokens": closed_tokens,
        "winning_tokens": winning_tokens,
        "median_pnl_per_token": float(statistics.median(pnls)),
        "repeatability_score": winning_tokens / closed_tokens if closed_tokens else None,
        "profit_hhi": sum(x * x for x in shares) if shares else None,
        "largest_win_share": shares[0] if shares else None,
        "top3_profit_share": sum(shares[:3]) if shares else None,
        "nansen_total_buys_30d": total_buys,
        "nansen_total_sells_30d": total_sells,
    }


def _normalized_metrics(summary: dict, pnl_rows: list[dict], *, chain: str) -> dict:
    win_rate = _float(summary.get("win_rate"))
    realized_pnl = _float(summary.get("realized_pnl_usd"))
    realized_roi = _float(summary.get("realized_pnl_percent"))
    traded_tokens = _int(summary.get("traded_token_count"), 0)
    traded_times = _int(summary.get("traded_times"), 0)
    distribution = _profit_distribution(pnl_rows)

    # A position sample is a closed token position, not the raw count of sales.
    # Nansen traded_times is preserved as total_trades metadata, while the Alpha
    # statistical gate uses closed token positions from the per-token PnL data.
    closed_positions = int(distribution.get("closed_tokens", 0) or 0)
    new_positions_per_week = (
        traded_tokens / (30.0 / 7.0) if traded_tokens > 0 else None
    )

    top5 = summary.get("top5_tokens") if isinstance(summary.get("top5_tokens"), list) else []
    return {
        "nansen_chain": chain,
        "nansen_evidence": True,
        "nansen_traded_token_count_30d": traded_tokens,
        "nansen_traded_times_30d": traded_times,
        "nansen_top5_tokens": json.dumps(top5, ensure_ascii=False, separators=(",", ":")),
        "win_rate": win_rate,
        "gmgn_winrate_30d": win_rate,
        "closed_positions": closed_positions,
        "total_trades": max(0, traded_times),
        "realized_profit_30d": realized_pnl,
        "realized_roi_30d": realized_roi,
        "new_positions_per_week": new_positions_per_week,
        "new_positions_per_week_source": "nansen_traded_token_count_30d_proxy",
        "tokens_traded": closed_positions,
        **distribution,
    }


def _fetch_wallet(
    api_key: str,
    address: str,
    chain: str,
    *,
    timeout: float,
    lookback_days: int,
) -> tuple[dict | None, dict]:
    dates = _date_range(lookback_days)
    calls = 0
    statuses = []
    credit_used = 0.0

    status, raw_summary, meta1 = _post(
        api_key,
        "/profiler/address/pnl-summary",
        {"wallet_address": address, "chain": chain, "date": dates},
        timeout,
    )
    calls += 1
    statuses.append(status)
    credit_used += _float(meta1.get("credits_used")) or 0.0
    if status != 200:
        return None, {"http_calls": calls, "statuses": statuses, "credits_used": credit_used}

    status2, raw_pnl, meta2 = _post(
        api_key,
        "/profiler/address/pnl",
        {
            "address": address,
            "chain": chain,
            "date": dates,
            "filters": {"show_realized": True},
            "pagination": {"page": 1, "per_page": 1000},
        },
        timeout,
    )
    calls += 1
    statuses.append(status2)
    credit_used += _float(meta2.get("credits_used")) or 0.0

    summary = _summary_payload(raw_summary)
    rows = _pnl_rows(raw_pnl) if status2 == 200 else []
    metrics = _normalized_metrics(summary, rows, chain=chain)
    metrics["nansen_detail_available"] = bool(status2 == 200)
    return metrics, {"http_calls": calls, "statuses": statuses, "credits_used": credit_used}


def enrich_nansen_pnl(
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
    output_path.parent.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    if not input_path.is_file():
        pd.DataFrame().to_csv(output_path, index=False)
        return {"status": "NO_INPUT", "wallets": 0, "enriched": 0, "http_calls": 0}
    try:
        frame = pd.read_csv(input_path)
    except Exception:
        frame = pd.DataFrame()
    if frame.empty or "address" not in frame.columns:
        frame.to_csv(output_path, index=False)
        return {"status": "DONE_EMPTY", "wallets": 0, "enriched": 0, "http_calls": 0}

    key = str(api_key or "").strip()
    if not key:
        frame.to_csv(output_path, index=False)
        return {"status": "NOT_CONFIGURED", "wallets": int(len(frame)), "enriched": 0, "http_calls": 0}

    cache_path = state_dir / f"nansen_pnl_{chain}_cache.json"
    cache = _load_json(cache_path)
    entries = cache.get("entries") if isinstance(cache.get("entries"), dict) else {}
    now = int(time.time())

    rows = frame.to_dict("records")
    enriched = errors = http_calls = 0
    credits_used = 0.0
    statuses: list[int] = []

    for row in rows[: max(0, int(max_wallets))]:
        address = str(row.get("address", "") or "").strip().lower()
        if not address:
            continue
        cached = entries.get(address) if isinstance(entries.get(address), dict) else {}
        fresh = (
            cached.get("metrics")
            and now - int(cached.get("checked_epoch", 0) or 0) < max(0, int(ttl_seconds))
        )
        if fresh:
            metrics = cached.get("metrics")
        else:
            metrics, meta = _fetch_wallet(
                key,
                address,
                chain,
                timeout=timeout,
                lookback_days=lookback_days,
            )
            http_calls += int(meta.get("http_calls", 0))
            credits_used += float(meta.get("credits_used", 0.0) or 0.0)
            statuses.extend(int(x) for x in meta.get("statuses", []) if x is not None)
            if metrics is None:
                errors += 1
                if delay > 0:
                    time.sleep(float(delay))
                continue
            entries[address] = {
                "checked_epoch": now,
                "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "metrics": metrics,
            }
            if delay > 0:
                time.sleep(float(delay))

        if isinstance(metrics, dict):
            row.update(metrics)
            row["wallet_evidence_state"] = "PERFORMANCE_ENRICHED"
            enriched += 1

    out = pd.DataFrame(rows)
    out.to_csv(output_path, index=False)
    _atomic_json(cache_path, {"entries": entries})
    return {
        "status": "DONE" if errors == 0 else ("PARTIAL" if enriched else "ERROR"),
        "chain": chain,
        "wallets": int(len(frame)),
        "attempted": min(int(len(frame)), max(0, int(max_wallets))),
        "enriched": int(enriched),
        "errors": int(errors),
        "http_calls": int(http_calls),
        "credits_used": round(float(credits_used), 4),
        "http_statuses": sorted(set(statuses)),
        "output": str(output_path),
    }
