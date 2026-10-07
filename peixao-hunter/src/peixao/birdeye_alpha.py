from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import math
import os
import time

import pandas as pd
import requests

from .units import RATIO, ROI_UNIT_KEY, percent_to_ratio, ratio_from_any


HARD_REJECT_TAGS = {"dev", "developer", "bundler", "insider", "chef", "known_deployer", "deployer", "airdrop_only"}
RISK_TAG_MARKERS = ("sniper", "bot", "arbitrage")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _num(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _tags(value) -> list[str]:
    if isinstance(value, list):
        return sorted({str(x).strip().lower() for x in value if str(x).strip()})
    if isinstance(value, str):
        return sorted({x.strip().lower() for x in value.replace(";", ",").split(",") if x.strip()})
    return []


def _top_items(payload) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return [x for x in data["items"] if isinstance(x, dict)]
    return []


def _find_number(obj, names: tuple[str, ...]):
    """Find a numeric value in shallow/nested provider payloads without assuming one schema revision."""
    if isinstance(obj, dict):
        for name in names:
            if name in obj:
                value = _num(obj.get(name))
                if value is not None:
                    return value
        for value in obj.values():
            if isinstance(value, dict):
                found = _find_number(value, names)
                if found is not None:
                    return found
    return None


def _summary_obj(payload) -> dict:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return {}
    summary = data.get("summary")
    return summary if isinstance(summary, dict) else data


def _detail_items(payload) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return []
    for key in ("items", "tokens", "details", "positions"):
        value = data.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    return []


def _profit_quality(items: list[dict]) -> dict:
    profits = []
    for item in items:
        value = _find_number(item, ("realized_profit", "realized_pnl", "realizedProfit", "realizedPnl", "profit_usd", "profitUsd"))
        if value is not None:
            profits.append(float(value))
    closed = len(profits)
    wins = [x for x in profits if x > 0]
    if not profits:
        return {}
    positive_total = sum(wins)
    shares = sorted([(x / positive_total) for x in wins], reverse=True) if positive_total > 0 else []
    hhi = sum(x * x for x in shares) if shares else None
    median = float(pd.Series(profits).median())
    return {
        "closed_positions": closed,
        "winning_tokens": len(wins),
        "median_pnl_per_token": median,
        "profit_hhi": hhi,
        "largest_win_share": shares[0] if shares else None,
        "top3_profit_share": sum(shares[:3]) if shares else None,
    }


def _parse_pnl(payload: dict, *, detail_status: str) -> dict:
    summary = _summary_obj(payload)
    details = _detail_items(payload)
    win_rate = ratio_from_any(_find_number(summary, ("win_rate", "winRate", "winrate")))
    unique_tokens = _find_number(summary, ("unique_tokens", "uniqueTokens", "token_num", "tokenNum", "tokens"))
    total_trades = _find_number(summary, ("total_trades", "totalTrades", "trade_count", "tradeCount", "trades"))
    total_win = _find_number(summary, ("total_win", "totalWin", "winning_tokens", "winningTokens"))
    total_loss = _find_number(summary, ("total_loss", "totalLoss", "losing_tokens", "losingTokens"))
    realized_profit = _find_number(summary, ("realized_profit", "realized_pnl", "realizedProfit", "realizedPnl"))
    # A unidade vem do nome do campo, nunca da magnitude: memecoins têm ROI
    # legítimo acima de 500%, e um ROI de 3% não pode virar 300%.
    realized_roi = _find_number(summary, ("realized_roi", "realizedRoi"))
    if realized_roi is None:
        realized_roi = percent_to_ratio(_find_number(summary, ("realized_profit_percent", "realizedProfitPercent")))

    quality = _profit_quality(details)
    closed_summary = None
    if total_win is not None or total_loss is not None:
        closed_summary = max(0.0, float(total_win or 0.0) + float(total_loss or 0.0))
    if not quality and closed_summary is not None:
        quality["closed_positions"] = closed_summary
        if total_win is not None:
            quality["winning_tokens"] = total_win

    result = {
        "birdeye_enrichment_status": detail_status,
        "win_rate": win_rate,
        "total_trades": total_trades,
        "realized_profit_30d": realized_profit,
        "realized_roi_30d": realized_roi,
        ROI_UNIT_KEY: RATIO,
        "tokens_traded": unique_tokens,
        "new_positions_per_week": (unique_tokens / (30.0 / 7.0)) if unique_tokens is not None else None,
        "new_positions_per_week_source": "birdeye_unique_tokens_30d_proxy" if unique_tokens is not None else "missing",
        **quality,
    }
    return result


def _fetch_top_traders(
    session: requests.Session,
    base_url: str,
    token: str,
    headers: dict,
    *,
    top_n: int,
    timeout: float,
    delay: float,
) -> tuple[list[dict], int, int | None]:
    rows = []
    calls = 0
    last_status = None
    for offset in range(0, max(0, int(top_n)), 10):
        limit = min(10, int(top_n) - offset)
        params = {
            "address": token,
            "time_frame": "30d",
            "sort_by": "realized_pnl",
            "sort_type": "desc",
            "offset": offset,
            "limit": limit,
            "ui_amount_mode": "scaled",
        }
        try:
            response = session.get(base_url.rstrip("/") + "/defi/v2/tokens/top_traders", headers=headers, params=params, timeout=timeout)
            calls += 1
            last_status = int(response.status_code)
            if response.status_code != 200:
                break
            items = _top_items(response.json())
            for i, item in enumerate(items):
                owner = str(item.get("owner", "") or "").strip()
                if not owner:
                    continue
                tags = _tags(item.get("tags"))
                rows.append({
                    "address": owner,
                    "token_address": token,
                    "token_rank": offset + i + 1,
                    "tags": tags,
                    "token_realized_pnl": _num(item.get("realizedPnl", item.get("realized_pnl"))),
                    "token_total_pnl": _num(item.get("totalPnl", item.get("total_pnl"))),
                    "token_volume_usd": _num(item.get("volumeUsd")),
                    "token_trade_count": _num(item.get("trade")),
                })
            if len(items) < limit:
                break
        except requests.RequestException:
            break
        if delay > 0:
            time.sleep(delay)
    return rows, calls, last_status


def _fetch_pnl(
    session: requests.Session,
    base_url: str,
    wallet: str,
    headers: dict,
    *,
    timeout: float,
) -> tuple[dict, int, int | None]:
    calls = 0
    try:
        response = session.post(
            base_url.rstrip("/") + "/wallet/v2/pnl/details",
            headers={**headers, "Content-Type": "application/json"},
            json={"wallet": wallet, "duration": "30d", "position_scope": "duration_only", "sort_by": "last_trade", "sort_type": "desc", "limit": 100, "offset": 0},
            timeout=timeout,
        )
        calls += 1
        if response.status_code == 200:
            return _parse_pnl(response.json(), detail_status="DETAILS_30D"), calls, 200
        detail_status = int(response.status_code)
    except requests.RequestException:
        detail_status = None

    # Standard-plan-safe fallback kept because this endpoint was already proven
    # usable in the earlier Peixao workflow.
    try:
        response = session.get(
            base_url.rstrip("/") + "/wallet/v2/pnl/summary",
            headers=headers,
            params={"wallet": wallet},
            timeout=timeout,
        )
        calls += 1
        if response.status_code == 200:
            return _parse_pnl(response.json(), detail_status="SUMMARY_FALLBACK"), calls, 200
        return {"birdeye_enrichment_status": f"HTTP_{response.status_code}"}, calls, int(response.status_code)
    except requests.RequestException:
        return {"birdeye_enrichment_status": f"DETAILS_HTTP_{detail_status}_FALLBACK_NETWORK_ERROR"}, calls, detail_status


def run_birdeye_alpha_discovery(
    shortlist_path: Path,
    output_dir: Path,
    state_dir: Path,
    *,
    api_key: str | None,
    base_url: str = "https://public-api.birdeye.so",
    timeout: float = 15.0,
    delay: float = 1.05,
    max_tokens: int = 5,
    top_traders_per_token: int = 30,
    min_cross_token_hits: int = 2,
    max_pnl_wallets: int = 20,
    top_trader_ttl_seconds: int = 43200,
    pnl_ttl_seconds: int = 86400,
) -> dict:
    """Spend Birdeye only after the zero-RPC radar has produced a tiny shortlist."""
    output_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    top_out = output_dir / "V22_radar_top_traders.csv"
    candidate_out = output_dir / "V22_radar_wallet_candidates.csv"
    enriched_out = output_dir / "V22_radar_wallet_enriched.csv"
    key = str(api_key or "").strip()
    if not key:
        for p in (top_out, candidate_out, enriched_out):
            pd.DataFrame().to_csv(p, index=False)
        return {"status": "NOT_CONFIGURED_BIRDEYE", "http_calls": 0, "tokens": 0, "recurrent_wallets": 0, "enriched_wallets": 0}
    if not shortlist_path.is_file():
        return {"status": "SKIPPED_NO_RADAR_SHORTLIST", "http_calls": 0, "tokens": 0, "recurrent_wallets": 0, "enriched_wallets": 0}
    try:
        shortlist = pd.read_csv(shortlist_path).head(max(0, int(max_tokens)))
    except pd.errors.EmptyDataError:
        shortlist = pd.DataFrame()
    if shortlist.empty or "token_address" not in shortlist.columns:
        for p in (top_out, candidate_out, enriched_out):
            pd.DataFrame().to_csv(p, index=False)
        return {"status": "DONE_EMPTY_RADAR", "http_calls": 0, "tokens": 0, "recurrent_wallets": 0, "enriched_wallets": 0}

    headers = {"X-API-KEY": key, "x-chain": "solana", "Accept": "application/json"}
    session = requests.Session()
    now = int(time.time())
    top_cache_path = state_dir / "birdeye_top_traders_cache.json"
    pnl_cache_path = state_dir / "birdeye_pnl_cache.json"
    top_cache = _load_json(top_cache_path)
    pnl_cache = _load_json(pnl_cache_path)
    top_entries = top_cache.get("tokens") if isinstance(top_cache.get("tokens"), dict) else {}
    pnl_entries = pnl_cache.get("wallets") if isinstance(pnl_cache.get("wallets"), dict) else {}
    http_calls = 0
    top_rows = []
    top_http_statuses = set()

    token_scores = {str(row.token_address): float(getattr(row, "radar_score", 0.0) or 0.0) for row in shortlist.itertuples()}
    for token in token_scores:
        cached = top_entries.get(token) if isinstance(top_entries.get(token), dict) else {}
        if cached.get("rows") and now - int(cached.get("checked_epoch", 0) or 0) < max(0, int(top_trader_ttl_seconds)):
            rows = cached.get("rows", [])
        else:
            rows, calls, http_status = _fetch_top_traders(session, base_url, token, headers, top_n=top_traders_per_token, timeout=timeout, delay=delay)
            http_calls += calls
            if http_status is not None:
                top_http_statuses.add(http_status)
            top_entries[token] = {"checked_epoch": now, "checked_at": _now_iso(), "rows": rows, "http_status": http_status}
        for row in rows:
            tags = _tags(row.get("tags"))
            row = {**row, "radar_score": token_scores.get(token, 0.0), "hard_reject": bool(HARD_REJECT_TAGS.intersection(tags)), "risk_tagged": any(any(m in tag for m in RISK_TAG_MARKERS) for tag in tags)}
            top_rows.append(row)
    _atomic_json(top_cache_path, {"tokens": top_entries})

    top_frame = pd.DataFrame(top_rows)
    top_frame.to_csv(top_out, index=False)
    if top_frame.empty:
        pd.DataFrame().to_csv(candidate_out, index=False)
        pd.DataFrame().to_csv(enriched_out, index=False)
        return {"status": "DONE_NO_TOP_TRADERS", "http_calls": http_calls, "tokens": len(token_scores), "recurrent_wallets": 0, "enriched_wallets": 0, "http_statuses": sorted(top_http_statuses)}

    clean = top_frame[top_frame["hard_reject"].eq(False)].copy()
    candidates = []
    for address, group in clean.groupby("address", sort=False):
        hits = int(group["token_address"].nunique())
        if hits < max(1, int(min_cross_token_hits)):
            continue
        signal = 0.0
        for row in group.itertuples():
            signal += (float(getattr(row, "radar_score", 0.0)) / 100.0) / math.sqrt(max(1.0, float(getattr(row, "token_rank", 1))))
        weighted_cross = min(1.0, signal / 1.20)
        all_tags = sorted({tag for tags in group["tags"] for tag in _tags(tags)})
        candidates.append({
            "address": address,
            "cross_token_hits": hits,
            "independent_cross_token_hits": hits,
            "weighted_cross_token_score": round(weighted_cross, 4),
            "best_token_rank": int(group["token_rank"].min()),
            "mean_token_rank": round(float(group["token_rank"].mean()), 2),
            "radar_tokens": "|".join(sorted(set(group["token_address"].astype(str)))),
            "risk_flags": "|".join(all_tags),
            "risk_tagged": bool(group["risk_tagged"].any()),
            "discovery_score": round(100.0 * weighted_cross, 2),
        })
    candidate_frame = pd.DataFrame(candidates)
    if not candidate_frame.empty:
        candidate_frame = candidate_frame.sort_values(["weighted_cross_token_score", "cross_token_hits", "best_token_rank"], ascending=[False, False, True]).reset_index(drop=True)
    candidate_frame.to_csv(candidate_out, index=False)

    enriched_rows = []
    for row in candidate_frame.head(max(0, int(max_pnl_wallets))).to_dict("records"):
        wallet = str(row["address"])
        cached = pnl_entries.get(wallet) if isinstance(pnl_entries.get(wallet), dict) else {}
        if cached.get("metrics") and now - int(cached.get("checked_epoch", 0) or 0) < max(0, int(pnl_ttl_seconds)):
            metrics = cached["metrics"]
        else:
            metrics, calls, http_status = _fetch_pnl(session, base_url, wallet, headers, timeout=timeout)
            http_calls += calls
            if delay > 0 and calls:
                time.sleep(delay)
            pnl_entries[wallet] = {"checked_epoch": now, "checked_at": _now_iso(), "metrics": metrics, "http_status": http_status}
        enriched_rows.append({**row, **metrics})
    _atomic_json(pnl_cache_path, {"wallets": pnl_entries})
    enriched = pd.DataFrame(enriched_rows)
    enriched.to_csv(enriched_out, index=False)
    return {
        "status": "DONE",
        "http_calls": int(http_calls),
        "tokens": int(len(token_scores)),
        "top_trader_rows": int(len(top_frame)),
        "clean_top_trader_rows": int(len(clean)),
        "recurrent_wallets": int(len(candidate_frame)),
        "enriched_wallets": int(len(enriched)),
        "output": str(enriched_out),
        "top_traders_output": str(top_out),
        "candidate_output": str(candidate_out),
        "top_http_statuses": sorted(top_http_statuses),
    }
