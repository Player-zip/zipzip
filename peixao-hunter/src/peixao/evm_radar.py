from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import json
import math
import os
import time

import pandas as pd
import requests

from .evidence_ledger import normalize_address, normalize_chain, wallet_key
from .rpc_budget import consume_daily_rpc


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _num(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


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


def _address(value) -> str:
    text = str(value or "").strip()
    if len(text) == 42 and text.lower().startswith("0x"):
        return text.lower()
    return ""


def _rpc(url: str, method: str, params: list, timeout: float) -> dict:
    if not consume_daily_rpc(url):
        raise RuntimeError("DAILY_RPC_BUDGET_EXHAUSTED")
    response = requests.post(
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=float(timeout),
    )
    response.raise_for_status()
    body = response.json()
    if isinstance(body, dict) and body.get("error"):
        raise RuntimeError(str(body.get("error")))
    return body if isinstance(body, dict) else {}


def _best_pairs(payload, chain_id: str) -> dict[str, dict]:
    rows = payload if isinstance(payload, list) else []
    best: dict[str, dict] = {}
    for pair in rows:
        if not isinstance(pair, dict):
            continue
        if str(pair.get("chainId", "")).lower() != chain_id.lower():
            continue
        base = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
        token = _address(base.get("address"))
        if not token:
            continue
        liquidity = pair.get("liquidity") if isinstance(pair.get("liquidity"), dict) else {}
        current = best.get(token)
        current_liq = 0.0
        if current:
            c_liq = current.get("liquidity") if isinstance(current.get("liquidity"), dict) else {}
            current_liq = _num(c_liq.get("usd"))
        if current is None or _num(liquidity.get("usd")) > current_liq:
            best[token] = pair
    return best


def _base_score(pair: dict, observed_at: str) -> dict:
    base = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
    txns = pair.get("txns") if isinstance(pair.get("txns"), dict) else {}
    h1 = txns.get("h1") if isinstance(txns.get("h1"), dict) else {}
    volume = pair.get("volume") if isinstance(pair.get("volume"), dict) else {}
    liquidity = pair.get("liquidity") if isinstance(pair.get("liquidity"), dict) else {}

    buys = _num(h1.get("buys"))
    sells = _num(h1.get("sells"))
    trades = buys + sells
    buy_pressure = buys / max(1.0, trades)
    liq_usd = _num(liquidity.get("usd"))
    vol24 = _num(volume.get("h24"))
    created_ms = _num(pair.get("pairCreatedAt"))
    age_s = max(0.0, time.time() - created_ms / 1000.0) if created_ms > 0 else None
    freshness = 0.0 if age_s is None else _clamp(1.0 - age_s / (30.0 * 86400.0))

    score = 100.0 * (
        0.30 * _clamp(math.log1p(max(0.0, liq_usd)) / math.log1p(750_000.0))
        + 0.30 * _clamp(math.log1p(max(0.0, vol24)) / math.log1p(2_000_000.0))
        + 0.20 * _clamp((buy_pressure - 0.35) / 0.45)
        + 0.10 * _clamp(math.log1p(max(0.0, trades)) / math.log1p(250.0))
        + 0.10 * freshness
    )

    return {
        "chain": "base",
        "chain_id": 8453,
        "token_address": _address(base.get("address")),
        "symbol": str(base.get("symbol", "") or "").upper(),
        "name": str(base.get("name", "") or ""),
        "observed_at": observed_at,
        "price_usd": _num(pair.get("priceUsd")) or None,
        "market_cap_usd": _num(pair.get("marketCap", pair.get("fdv"))) or None,
        "liquidity_usd": liq_usd or None,
        "volume_24h_usd": vol24 or None,
        "buys_h1": buys,
        "sells_h1": sells,
        "buy_pressure_h1": round(buy_pressure, 4),
        "token_age_seconds": None if age_s is None else round(age_s, 2),
        "radar_score": round(score, 2),
        "radar_source": "DEXSCREENER_BASE",
        "pair_address": pair.get("pairAddress"),
        "dex_id": pair.get("dexId"),
    }


def _alchemy_token_transfers(
    rpc_url: str,
    token: str,
    *,
    timeout: float,
    max_count: int = 100,
) -> list[dict]:
    count = max(1, min(int(max_count), 1000))
    body = _rpc(
        rpc_url,
        "alchemy_getAssetTransfers",
        [{
            "fromBlock": "0x0",
            "toBlock": "latest",
            "contractAddresses": [token],
            "category": ["erc20"],
            "excludeZeroValue": True,
            "withMetadata": True,
            "order": "desc",
            "maxCount": hex(count),
        }],
        timeout,
    )
    result = body.get("result") if isinstance(body.get("result"), dict) else {}
    rows = result.get("transfers") if isinstance(result.get("transfers"), list) else []
    return [x for x in rows if isinstance(x, dict)]


def _is_eoa(rpc_url: str, wallet: str, timeout: float) -> bool:
    try:
        body = _rpc(rpc_url, "eth_getCode", [wallet, "latest"], timeout)
        code = str(body.get("result", "") or "").lower()
        return code in {"0x", "0x0", ""}
    except Exception:
        return False


def discover_wallets_from_tokens(
    shortlist: pd.DataFrame,
    *,
    chain: str,
    chain_id: int,
    alchemy_rpc_url: str | None,
    output_path: Path,
    timeout: float = 15.0,
    min_cross_token_hits: int = 2,
    max_wallets: int = 50,
    transfers_per_token: int = 100,
) -> dict:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if shortlist.empty or "token_address" not in shortlist.columns:
        pd.DataFrame().to_csv(output_path, index=False)
        return {"status": "DONE_EMPTY", "wallets": 0, "http_calls": 0, "tokens": 0}
    if not alchemy_rpc_url:
        pd.DataFrame().to_csv(output_path, index=False)
        return {"status": "NOT_CONFIGURED_ALCHEMY", "wallets": 0, "http_calls": 0, "tokens": int(len(shortlist))}

    token_sets: dict[str, set[str]] = defaultdict(set)
    event_counts: dict[str, int] = defaultdict(int)
    http_calls = 0
    errors = 0

    tokens = [_address(x) for x in shortlist["token_address"].astype(str).tolist()]
    tokens = [x for x in tokens if x]
    token_universe = set(tokens)

    for token in tokens:
        try:
            transfers = _alchemy_token_transfers(
                alchemy_rpc_url,
                token,
                timeout=timeout,
                max_count=transfers_per_token,
            )
            http_calls += 1
        except Exception:
            errors += 1
            continue
        for tx in transfers:
            for key in ("from", "to"):
                wallet = _address(tx.get(key))
                if not wallet or wallet == ZERO_ADDRESS or wallet in token_universe:
                    continue
                token_sets[wallet].add(token)
                event_counts[wallet] += 1

    ranked = sorted(
        token_sets,
        key=lambda w: (len(token_sets[w]), event_counts[w]),
        reverse=True,
    )
    ranked = [w for w in ranked if len(token_sets[w]) >= max(1, int(min_cross_token_hits))]
    ranked = ranked[: max(1, int(max_wallets)) * 4]

    rows = []
    eoa_checks = 0
    for wallet in ranked:
        if len(rows) >= max(0, int(max_wallets)):
            break
        eoa_checks += 1
        if not _is_eoa(alchemy_rpc_url, wallet, timeout):
            continue
        hits = len(token_sets[wallet])
        events = event_counts[wallet]
        discovery_score = min(100.0, 35.0 + 15.0 * hits + 2.0 * math.log1p(events))
        rows.append({
            "address": wallet,
            "chain": chain,
            "chain_id": int(chain_id),
            "discovery_source": "ALCHEMY_ASSET_TRANSFERS",
            "independent_cross_token_hits": int(hits),
            "distinct_tokens": int(hits),
            "total_transfer_events": int(events),
            "discovery_score": round(discovery_score, 2),
            "classification": "EOA_NO_CODE",
            "wallet_evidence_state": "DISCOVERY_ONLY",
        })

    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame = frame.sort_values(
            ["independent_cross_token_hits", "total_transfer_events", "discovery_score"],
            ascending=[False, False, False],
            kind="mergesort",
        ).reset_index(drop=True)
    frame.to_csv(output_path, index=False)
    return {
        "status": "DONE" if errors == 0 else "PARTIAL",
        "wallets": int(len(frame)),
        "http_calls": int(http_calls + eoa_checks),
        "tokens": int(len(tokens)),
        "transfer_errors": int(errors),
        "output": str(output_path),
    }


def run_base_radar(
    output_dir: Path,
    state_dir: Path,
    *,
    dexscreener_base_url: str = "https://api.dexscreener.com",
    alchemy_api_key: str | None = None,
    timeout: float = 15.0,
    ttl_seconds: int = 1800,
    max_candidates: int = 50,
    max_shortlist: int = 5,
    min_liquidity_usd: float = 25_000.0,
    min_volume_24h_usd: float = 20_000.0,
    min_radar_score: float = 45.0,
    min_cross_token_hits: int = 2,
    max_wallets: int = 50,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "V22_base_token_radar.csv"
    shortlist_path = output_dir / "V22_base_token_radar_shortlist.csv"
    wallet_path = output_dir / "V22_base_wallet_candidates.csv"
    cache_path = state_dir / "base_token_radar_cache.json"

    previous = _load_json(cache_path)
    now = int(time.time())
    cached = bool(previous.get("rows") and now - int(previous.get("checked_epoch", 0) or 0) < max(0, int(ttl_seconds)))
    http_calls = 0

    if cached:
        frame = pd.DataFrame(previous.get("rows", []))
    else:
        observed_at = _now_iso()
        profiles: list[dict] = []
        try:
            response = requests.get(
                dexscreener_base_url.rstrip("/") + "/token-profiles/latest/v1",
                headers={"Accept": "application/json"},
                timeout=float(timeout),
            )
            http_calls += 1
            response.raise_for_status()
            payload = response.json()
            raw = payload if isinstance(payload, list) else []
            profiles = [
                x for x in raw
                if isinstance(x, dict)
                and str(x.get("chainId", "")).lower() == "base"
                and _address(x.get("tokenAddress"))
            ][: max(1, int(max_candidates))]
        except Exception:
            profiles = []

        addresses = [_address(x.get("tokenAddress")) for x in profiles]
        best: dict[str, dict] = {}
        for start in range(0, len(addresses), 30):
            chunk = addresses[start:start + 30]
            if not chunk:
                continue
            try:
                response = requests.get(
                    dexscreener_base_url.rstrip("/") + "/tokens/v1/base/" + ",".join(chunk),
                    headers={"Accept": "application/json"},
                    timeout=float(timeout),
                )
                http_calls += 1
                if 200 <= response.status_code < 300:
                    best.update(_best_pairs(response.json(), "base"))
            except Exception:
                continue

        rows = [_base_score(pair, observed_at) for pair in best.values()]
        frame = pd.DataFrame(rows)
        _atomic_json(cache_path, {"checked_epoch": now, "checked_at": observed_at, "rows": rows})

    if frame.empty:
        frame.to_csv(out_path, index=False)
        pd.DataFrame().to_csv(shortlist_path, index=False)
        pd.DataFrame().to_csv(wallet_path, index=False)
        return {"status": "DONE_EMPTY", "cached": cached, "tokens_seen": 0, "shortlisted": 0, "wallets": 0, "http_calls": http_calls}

    frame = frame.sort_values("radar_score", ascending=False, kind="mergesort").reset_index(drop=True)
    qualified = frame[
        frame["liquidity_usd"].fillna(0).ge(float(min_liquidity_usd))
        & frame["volume_24h_usd"].fillna(0).ge(float(min_volume_24h_usd))
        & frame["radar_score"].fillna(0).ge(float(min_radar_score))
    ].head(max(0, int(max_shortlist))).copy()
    selected = set(qualified["token_address"].astype(str))
    frame["radar_selected"] = frame["token_address"].astype(str).isin(selected)
    frame.to_csv(out_path, index=False)
    qualified.to_csv(shortlist_path, index=False)

    key = str(alchemy_api_key or "").strip()
    alchemy_url = f"https://base-mainnet.g.alchemy.com/v2/{key}" if key else None
    wallets = discover_wallets_from_tokens(
        qualified,
        chain="base",
        chain_id=8453,
        alchemy_rpc_url=alchemy_url,
        output_path=wallet_path,
        timeout=timeout,
        min_cross_token_hits=min_cross_token_hits,
        max_wallets=max_wallets,
    )

    return {
        "status": "DONE" if wallets.get("status") in {"DONE", "DONE_EMPTY", "NOT_CONFIGURED_ALCHEMY"} else "PARTIAL",
        "cached": cached,
        "tokens_seen": int(len(frame)),
        "shortlisted": int(len(qualified)),
        "wallets": int(wallets.get("wallets", 0)),
        "http_calls": int(http_calls + wallets.get("http_calls", 0)),
        "wallet_discovery_status": wallets.get("status"),
        "output": str(out_path),
        "shortlist_output": str(shortlist_path),
        "wallet_output": str(wallet_path),
    }


def _robinhood_assets(payload) -> list[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("assets", "results", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def _robinhood_contract(asset: dict) -> str:
    deployments = asset.get("deployments") if isinstance(asset.get("deployments"), list) else []
    for dep in deployments:
        if not isinstance(dep, dict):
            continue
        try:
            chain_id = int(dep.get("chainId"))
        except Exception:
            continue
        if chain_id == 4663:
            address = _address(dep.get("contractAddress"))
            if address:
                return address
    return ""


def run_robinhood_radar(
    output_dir: Path,
    state_dir: Path,
    *,
    alchemy_api_key: str | None = None,
    assets_base_url: str = "https://api.robinhood.com/rhj",
    timeout: float = 15.0,
    ttl_seconds: int = 1800,
    max_candidates: int = 30,
    max_shortlist: int = 5,
    min_cross_token_hits: int = 2,
    max_wallets: int = 50,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "V22_robinhood_token_radar.csv"
    shortlist_path = output_dir / "V22_robinhood_token_radar_shortlist.csv"
    wallet_path = output_dir / "V22_robinhood_wallet_candidates.csv"
    cache_path = state_dir / "robinhood_token_radar_cache.json"

    previous = _load_json(cache_path)
    now = int(time.time())
    cached = bool(previous.get("rows") and now - int(previous.get("checked_epoch", 0) or 0) < max(0, int(ttl_seconds)))
    http_calls = 0

    if cached:
        frame = pd.DataFrame(previous.get("rows", []))
    else:
        observed_at = _now_iso()
        assets: list[dict] = []
        try:
            response = requests.get(
                assets_base_url.rstrip("/") + "/assets",
                headers={"Accept": "application/json"},
                timeout=float(timeout),
            )
            http_calls += 1
            response.raise_for_status()
            assets = [
                x for x in _robinhood_assets(response.json())
                if str(x.get("status", "")).upper() in {"ASSET_STATUS_ACTIVE", "ACTIVE", ""}
                and _robinhood_contract(x)
            ][: max(1, int(max_candidates))]
        except Exception:
            assets = []

        rows = []
        for asset in assets:
            symbol = str(asset.get("tokenSymbol", "") or "").upper()
            address = _robinhood_contract(asset)
            daily_volume = 0.0
            bid = ask = None
            if symbol:
                try:
                    response = requests.get(
                        assets_base_url.rstrip("/") + "/prices/" + symbol,
                        headers={"Accept": "application/json"},
                        timeout=float(timeout),
                    )
                    http_calls += 1
                    if 200 <= response.status_code < 300:
                        body = response.json()
                        quotes = body.get("quotes") if isinstance(body, dict) and isinstance(body.get("quotes"), list) else []
                        quote = quotes[0] if quotes and isinstance(quotes[0], dict) else {}
                        daily_volume = _num(quote.get("dailyTradingVolume"))
                        bid = _num(quote.get("bid")) or None
                        ask = _num(quote.get("ask")) or None
                except Exception:
                    pass
            score = 100.0 * _clamp(math.log1p(max(0.0, daily_volume)) / math.log1p(200_000_000.0))
            rows.append({
                "chain": "robinhood",
                "chain_id": 4663,
                "token_address": address,
                "symbol": symbol,
                "name": str(asset.get("tokenName", "") or ""),
                "observed_at": observed_at,
                "underlying_bid_usd": bid,
                "underlying_ask_usd": ask,
                "underlying_daily_volume": daily_volume or None,
                "radar_score": round(score, 2),
                "radar_source": "ROBINHOOD_ASSETS_PRICES",
                "selection_basis": "UNDERLYING_VOLUME_SEED_ONLY",
            })
        frame = pd.DataFrame(rows)
        _atomic_json(cache_path, {"checked_epoch": now, "checked_at": observed_at, "rows": rows})

    if frame.empty:
        frame.to_csv(out_path, index=False)
        pd.DataFrame().to_csv(shortlist_path, index=False)
        pd.DataFrame().to_csv(wallet_path, index=False)
        return {"status": "DONE_EMPTY", "cached": cached, "tokens_seen": 0, "shortlisted": 0, "wallets": 0, "http_calls": http_calls}

    frame = frame.sort_values("radar_score", ascending=False, kind="mergesort").reset_index(drop=True)
    qualified = frame.head(max(0, int(max_shortlist))).copy()
    selected = set(qualified["token_address"].astype(str))
    frame["radar_selected"] = frame["token_address"].astype(str).isin(selected)
    frame.to_csv(out_path, index=False)
    qualified.to_csv(shortlist_path, index=False)

    key = str(alchemy_api_key or "").strip()
    alchemy_url = f"https://robinhood-mainnet.g.alchemy.com/v2/{key}" if key else None
    wallets = discover_wallets_from_tokens(
        qualified,
        chain="robinhood",
        chain_id=4663,
        alchemy_rpc_url=alchemy_url,
        output_path=wallet_path,
        timeout=timeout,
        min_cross_token_hits=min_cross_token_hits,
        max_wallets=max_wallets,
    )

    return {
        "status": "DONE" if wallets.get("status") in {"DONE", "DONE_EMPTY", "NOT_CONFIGURED_ALCHEMY"} else "PARTIAL",
        "cached": cached,
        "tokens_seen": int(len(frame)),
        "shortlisted": int(len(qualified)),
        "wallets": int(wallets.get("wallets", 0)),
        "http_calls": int(http_calls + wallets.get("http_calls", 0)),
        "wallet_discovery_status": wallets.get("status"),
        "output": str(out_path),
        "shortlist_output": str(shortlist_path),
        "wallet_output": str(wallet_path),
    }


def merge_multichain_wallet_inputs(
    solana_path: Path | None,
    base_path: Path | None,
    robinhood_path: Path | None,
    output_path: Path,
) -> dict:
    frames: list[pd.DataFrame] = []
    for chain, path in (
        ("solana", solana_path),
        ("base", base_path),
        ("robinhood", robinhood_path),
    ):
        if path is None or not path.is_file():
            continue
        try:
            frame = pd.read_csv(path)
        except Exception:
            continue
        if frame.empty or "address" not in frame.columns:
            continue
        frame = frame.copy()
        if "chain" not in frame.columns:
            frame["chain"] = chain
        else:
            frame["chain"] = frame["chain"].fillna(chain).astype(str).replace("", chain)
        frames.append(frame)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not frames:
        pd.DataFrame().to_csv(output_path, index=False)
        return {"status": "DONE_EMPTY", "wallets": 0, "by_chain": {}, "output": str(output_path)}

    out = pd.concat(frames, ignore_index=True, sort=False)
    out["chain"] = out["chain"].map(normalize_chain)
    out["address"] = [normalize_address(c, a) for c, a in zip(out["chain"], out["address"], strict=True)]
    out = out[out["address"].ne("") & out["address"].str.lower().ne("nan")].copy()
    # Identidade é chain:address: a mesma wallet EVM em Base e Robinhood são
    # duas linhas, cada uma com a própria evidência.
    out["wallet_key"] = [wallet_key(c, a) for c, a in zip(out["chain"], out["address"], strict=True)]
    out["_evidence_count"] = out.notna().sum(axis=1)
    out = out.sort_values(["wallet_key", "_evidence_count"], ascending=[True, False], kind="mergesort")
    out = out.drop_duplicates("wallet_key", keep="first").drop(columns=["_evidence_count"]).reset_index(drop=True)
    out.to_csv(output_path, index=False)
    by_chain = {str(k): int(v) for k, v in out["chain"].fillna("unknown").value_counts().to_dict().items()}
    return {"status": "DONE", "wallets": int(len(out)), "by_chain": by_chain, "output": str(output_path)}
