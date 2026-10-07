from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import math
import os
import time

import pandas as pd
import requests

from .radar_db import record_token_signal_snapshots


STABLE_SYMBOLS = {"USDC", "USDT", "USDS", "PYUSD", "EURC", "USDG", "DAI"}
CORE_MINTS = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
}
DEX_DISCOVERY_ENDPOINTS = (
    ("profile", "/token-profiles/latest/v1"),
    ("boost_latest", "/token-boosts/latest/v1"),
    ("boost_top", "/token-boosts/top/v1"),
    ("community_takeover", "/community-takeovers/latest/v1"),
)


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


def _num(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _organic_100(item: dict) -> float:
    raw = _num(item.get("organicScore", item.get("organic_score", 0.0)))
    return max(0.0, min(100.0, raw * 100.0 if 0.0 <= raw <= 1.0 else raw))


def _jupiter_items(payload) -> list[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("tokens", "data", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def _rows(payload) -> list[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def _token_address(item: dict) -> str:
    for key in ("id", "address", "mint", "tokenAddress", "token_address"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _dex_best_pairs(payload) -> dict[str, dict]:
    rows = payload if isinstance(payload, list) else (payload.get("pairs", []) if isinstance(payload, dict) else [])
    best: dict[str, dict] = {}
    for pair in rows:
        if not isinstance(pair, dict) or str(pair.get("chainId", "")).lower() != "solana":
            continue
        base = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
        address = str(base.get("address", "") or "").strip()
        if not address:
            continue
        liquidity = pair.get("liquidity") if isinstance(pair.get("liquidity"), dict) else {}
        liq = _num(liquidity.get("usd"))
        previous = best.get(address, {})
        previous_liquidity = previous.get("liquidity") if isinstance(previous.get("liquidity"), dict) else {}
        if address not in best or liq > _num(previous_liquidity.get("usd")):
            best[address] = pair
    return best


def _score_row(item: dict, pair: dict | None, observed_at: str, *, market_only: bool = False) -> dict:
    address = _token_address(item)
    pair = pair or {}
    base = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
    symbol = str(item.get("symbol", base.get("symbol", "")) or "").upper()
    holders = _num(item.get("holderCount", item.get("holders", 0.0)))
    organic = _organic_100(item)
    liquidity = pair.get("liquidity") if isinstance(pair.get("liquidity"), dict) else {}
    volume = pair.get("volume") if isinstance(pair.get("volume"), dict) else {}
    txns = pair.get("txns") if isinstance(pair.get("txns"), dict) else {}
    h1 = txns.get("h1") if isinstance(txns.get("h1"), dict) else {}

    liq_usd = _num(liquidity.get("usd"))
    vol24 = _num(volume.get("h24"))
    buys_h1 = _num(h1.get("buys"))
    sells_h1 = _num(h1.get("sells"))
    buy_pressure = buys_h1 / max(1.0, buys_h1 + sells_h1)
    market_cap = _num(pair.get("marketCap", pair.get("fdv")))
    price_usd = _num(pair.get("priceUsd"))
    created_ms = _num(pair.get("pairCreatedAt"))
    age_s = max(0.0, time.time() - created_ms / 1000.0) if created_ms > 0 else None

    if market_only:
        # No Jupiter secret is required for this fallback. Weight only observable
        # DEX market quality, while keeping the same 0-100 radar scale.
        score = 100.0 * (
            0.45 * _clamp(math.log1p(max(0.0, liq_usd)) / math.log1p(500_000.0))
            + 0.35 * _clamp(math.log1p(max(0.0, vol24)) / math.log1p(1_000_000.0))
            + 0.20 * _clamp((buy_pressure - 0.35) / 0.45)
        )
        radar_source = "DEXSCREENER_MARKET_FALLBACK"
    else:
        score = 100.0 * (
            0.40 * _clamp(organic / 100.0)
            + 0.25 * _clamp(math.log1p(max(0.0, liq_usd)) / math.log1p(500_000.0))
            + 0.20 * _clamp(math.log1p(max(0.0, vol24)) / math.log1p(1_000_000.0))
            + 0.10 * _clamp((buy_pressure - 0.35) / 0.45)
            + 0.05 * _clamp(math.log1p(max(0.0, holders)) / math.log1p(5_000.0))
        )
        radar_source = "JUPITER+DEXSCREENER"

    return {
        "chain": "solana",
        "token_address": address,
        "symbol": symbol,
        "observed_at": observed_at,
        "jupiter_organic_score": round(organic, 4) if not market_only else None,
        "holders": holders or None,
        "price_usd": price_usd or None,
        "market_cap_usd": market_cap or None,
        "liquidity_usd": liq_usd or None,
        "volume_24h_usd": vol24 or None,
        "buys_h1": buys_h1,
        "sells_h1": sells_h1,
        "buy_pressure_h1": round(buy_pressure, 4),
        "token_age_seconds": None if age_s is None else round(age_s, 2),
        "radar_score": round(score, 2),
        "radar_source": radar_source,
        "pair_address": pair.get("pairAddress"),
        "dex_id": pair.get("dexId"),
    }


def _dexscreener_seed_items(base_url: str, *, timeout: float, max_candidates: int) -> tuple[list[dict], int, dict[str, int]]:
    candidates: dict[str, dict] = {}
    calls = 0
    source_counts: dict[str, int] = {}
    base_url = base_url.rstrip("/")

    for source, endpoint in DEX_DISCOVERY_ENDPOINTS:
        found = 0
        try:
            response = requests.get(
                base_url + endpoint,
                headers={"Accept": "application/json"},
                timeout=float(timeout),
            )
            calls += 1
            response.raise_for_status()
            for row in _rows(response.json()):
                if str(row.get("chainId", "")).lower() != "solana":
                    continue
                address = str(row.get("tokenAddress", "") or "").strip()
                if not address or address in CORE_MINTS:
                    continue
                item = candidates.setdefault(address, {"id": address, "discovery_feeds": set()})
                item["discovery_feeds"].add(source)
                found += 1
        except Exception:
            pass
        source_counts[source] = found

    ranked = sorted(
        candidates.values(),
        key=lambda item: len(item.get("discovery_feeds", set())),
        reverse=True,
    )[: max(1, int(max_candidates))]
    clean = []
    for item in ranked:
        out = dict(item)
        feeds = sorted(out.pop("discovery_feeds", set()))
        out["discovery_feeds"] = ",".join(feeds)
        out["discovery_feed_count"] = len(feeds)
        clean.append(out)
    return clean, calls, source_counts


def run_token_radar(
    output_dir: Path,
    state_dir: Path,
    db_path: Path,
    *,
    jupiter_api_key: str | None,
    jupiter_base_url: str = "https://api.jup.ag",
    dexscreener_base_url: str = "https://api.dexscreener.com",
    timeout: float = 10.0,
    ttl_seconds: int = 1800,
    max_candidates: int = 50,
    max_shortlist: int = 5,
    min_liquidity_usd: float = 25_000.0,
    min_volume_24h_usd: float = 20_000.0,
    min_radar_score: float = 45.0,
) -> dict:
    """Solana radar: Jupiter+DEX when configured, public DEX discovery otherwise."""
    output_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "V22_token_radar.csv"
    shortlist_path = output_dir / "V22_token_radar_shortlist.csv"
    cache_path = state_dir / "token_radar_cache.json"
    key = str(jupiter_api_key or "").strip()

    previous = _load_json(cache_path)
    now = int(time.time())
    source_counts: dict[str, int] = {}
    if previous.get("rows") and now - int(previous.get("checked_epoch", 0) or 0) < max(0, int(ttl_seconds)):
        frame = pd.DataFrame(previous["rows"])
        cached = True
        http_calls = 0
        discovery_mode = str(previous.get("discovery_mode", "jupiter" if key else "dexscreener"))
        source_counts = dict(previous.get("source_counts") or {})
    else:
        cached = False
        http_calls = 0
        discovery_mode = "jupiter" if key else "dexscreener"
        items: list[dict] = []

        if key:
            try:
                response = requests.get(
                    jupiter_base_url.rstrip("/") + "/tokens/v2/toporganicscore/5m",
                    headers={"x-api-key": key, "Accept": "application/json"},
                    timeout=float(timeout),
                )
                http_calls += 1
                response.raise_for_status()
                items = _jupiter_items(response.json())[: max(1, int(max_candidates))]
            except requests.RequestException:
                items = []
        else:
            items, discovery_calls, source_counts = _dexscreener_seed_items(
                dexscreener_base_url,
                timeout=timeout,
                max_candidates=max_candidates,
            )
            http_calls += discovery_calls

        clean_items = []
        for item in items:
            address = _token_address(item)
            symbol = str(item.get("symbol", "") or "").upper()
            if not address or address in CORE_MINTS or symbol in STABLE_SYMBOLS:
                continue
            clean_items.append(item)

        best_pairs: dict[str, dict] = {}
        addresses = [_token_address(x) for x in clean_items]
        for start in range(0, len(addresses), 30):
            chunk = addresses[start : start + 30]
            if not chunk:
                continue
            try:
                dex = requests.get(
                    dexscreener_base_url.rstrip("/") + "/tokens/v1/solana/" + ",".join(chunk),
                    headers={"Accept": "application/json"},
                    timeout=float(timeout),
                )
                http_calls += 1
                if 200 <= dex.status_code < 300:
                    best_pairs.update(_dex_best_pairs(dex.json()))
            except requests.RequestException:
                continue

        observed_at = _now_iso()
        rows = []
        for item in clean_items:
            pair = best_pairs.get(_token_address(item))
            row = _score_row(item, pair, observed_at, market_only=not bool(key))
            if not key:
                row["discovery_feeds"] = item.get("discovery_feeds", "")
                row["discovery_feed_count"] = int(item.get("discovery_feed_count", 0) or 0)
            rows.append(row)
        frame = pd.DataFrame(rows)
        _atomic_json(
            cache_path,
            {
                "checked_epoch": now,
                "checked_at": observed_at,
                "discovery_mode": discovery_mode,
                "source_counts": source_counts,
                "rows": rows,
            },
        )

    if frame.empty:
        frame.to_csv(out_path, index=False)
        frame.to_csv(shortlist_path, index=False)
        return {
            "status": "DONE_EMPTY",
            "cached": cached,
            "discovery_mode": discovery_mode,
            "http_calls": http_calls,
            "rpc_calls": 0,
            "tokens_seen": 0,
            "shortlisted": 0,
            "source_counts": source_counts,
        }

    sort_cols = ["radar_score"]
    ascending = [False]
    if "discovery_feed_count" in frame.columns:
        sort_cols.append("discovery_feed_count")
        ascending.append(False)
    frame = frame.sort_values(sort_cols, ascending=ascending, kind="mergesort").reset_index(drop=True)
    qualified = frame[
        frame["liquidity_usd"].fillna(0).ge(float(min_liquidity_usd))
        & frame["volume_24h_usd"].fillna(0).ge(float(min_volume_24h_usd))
        & frame["radar_score"].fillna(0).ge(float(min_radar_score))
    ].head(max(0, int(max_shortlist))).copy()
    selected = set(qualified["token_address"].astype(str))
    frame["radar_selected"] = frame["token_address"].astype(str).isin(selected)
    frame.to_csv(out_path, index=False)
    qualified.to_csv(shortlist_path, index=False)
    snapshot_summary = record_token_signal_snapshots(frame, db_path)
    return {
        "status": "DONE",
        "cached": cached,
        "discovery_mode": discovery_mode,
        "http_calls": int(http_calls),
        "rpc_calls": 0,
        "tokens_seen": int(len(frame)),
        "qualified": int(frame["radar_selected"].sum()),
        "shortlisted": int(len(qualified)),
        "source_counts": source_counts,
        "snapshots_inserted": int(snapshot_summary.get("snapshots_inserted", 0)),
        "output": str(out_path),
        "shortlist_output": str(shortlist_path),
    }
