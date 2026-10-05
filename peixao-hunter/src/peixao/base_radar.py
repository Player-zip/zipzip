from __future__ import annotations

from pathlib import Path
import time

import pandas as pd
import requests

from .evm_radar import (
    _address,
    _atomic_json,
    _base_score,
    _best_pairs,
    _load_json,
    _now_iso,
    discover_wallets_from_tokens,
)


BASE_DISCOVERY_ENDPOINTS = (
    ("profile", "/token-profiles/latest/v1"),
    ("boost_latest", "/token-boosts/latest/v1"),
    ("boost_top", "/token-boosts/top/v1"),
    ("community_takeover", "/community-takeovers/latest/v1"),
)


def _rows(payload) -> list[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def _blockscout_seed_addresses(
    base_url: str,
    *,
    timeout: float,
    max_items: int,
) -> tuple[list[str], int]:
    """Get a broad Base ERC-20 seed set from Blockscout's public v2 API.

    These are discovery seeds only. A token still has to have a live Base DEX
    pair and pass the same liquidity/volume/radar filters before wallet work.
    """
    addresses: list[str] = []
    calls = 0
    params: dict = {"type": "ERC-20"}
    seen: set[str] = set()

    # Two pages is deliberately bounded; DexScreener does the market-quality
    # validation afterwards, so this does not turn the explorer into a score.
    for _ in range(2):
        try:
            response = requests.get(
                base_url.rstrip("/") + "/api/v2/tokens",
                params=params,
                headers={"Accept": "application/json"},
                timeout=float(timeout),
            )
            calls += 1
            response.raise_for_status()
            body = response.json()
        except Exception:
            break

        items = body.get("items") if isinstance(body, dict) and isinstance(body.get("items"), list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            token = _address(item.get("address") or item.get("address_hash"))
            if token and token not in seen:
                seen.add(token)
                addresses.append(token)
                if len(addresses) >= max(1, int(max_items)):
                    return addresses, calls

        next_params = body.get("next_page_params") if isinstance(body, dict) else None
        if not isinstance(next_params, dict) or not next_params:
            break
        params = {"type": "ERC-20", **next_params}

    return addresses, calls


def run_base_radar(
    output_dir: Path,
    state_dir: Path,
    *,
    dexscreener_base_url: str = "https://api.dexscreener.com",
    blockscout_base_url: str = "https://base.blockscout.com",
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
    """Live Base radar: broad discovery -> DEX validation -> wallet recurrence.

    DexScreener discovery feeds and Blockscout are only seed sources. Market
    filters still come from Base pair data, and discovery-only wallets cannot
    pass Selective Alpha without real WR/PnL/performance evidence downstream.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "V22_base_token_radar.csv"
    shortlist_path = output_dir / "V22_base_token_radar_shortlist.csv"
    wallet_path = output_dir / "V22_base_wallet_candidates.csv"
    cache_path = state_dir / "base_token_radar_v3_cache.json"

    previous = _load_json(cache_path)
    now = int(time.time())
    cached = bool(
        previous.get("rows")
        and now - int(previous.get("checked_epoch", 0) or 0) < max(0, int(ttl_seconds))
    )
    http_calls = 0
    source_counts: dict[str, int] = {}

    if cached:
        frame = pd.DataFrame(previous.get("rows", []))
        source_counts = dict(previous.get("source_counts") or {})
    else:
        observed_at = _now_iso()
        candidates: dict[str, set[str]] = {}
        base_url = dexscreener_base_url.rstrip("/")

        for source, endpoint in BASE_DISCOVERY_ENDPOINTS:
            found = 0
            try:
                response = requests.get(
                    base_url + endpoint,
                    headers={"Accept": "application/json"},
                    timeout=float(timeout),
                )
                http_calls += 1
                response.raise_for_status()
                for item in _rows(response.json()):
                    if str(item.get("chainId", "")).lower() != "base":
                        continue
                    token = _address(item.get("tokenAddress"))
                    if not token:
                        continue
                    candidates.setdefault(token, set()).add(source)
                    found += 1
            except Exception:
                pass
            source_counts[source] = found

        explorer_seeds, explorer_calls = _blockscout_seed_addresses(
            blockscout_base_url,
            timeout=timeout,
            max_items=max(50, int(max_candidates) * 2),
        )
        http_calls += explorer_calls
        source_counts["blockscout_tokens"] = len(explorer_seeds)
        for token in explorer_seeds:
            candidates.setdefault(token, set()).add("blockscout_tokens")

        ranked_addresses = sorted(
            candidates,
            key=lambda addr: (len(candidates[addr]), "blockscout_tokens" not in candidates[addr]),
            reverse=True,
        )[: max(1, int(max_candidates))]

        best: dict[str, dict] = {}
        for start in range(0, len(ranked_addresses), 30):
            chunk = ranked_addresses[start:start + 30]
            if not chunk:
                continue
            try:
                response = requests.get(
                    base_url + "/tokens/v1/base/" + ",".join(chunk),
                    headers={"Accept": "application/json"},
                    timeout=float(timeout),
                )
                http_calls += 1
                if 200 <= response.status_code < 300:
                    best.update(_best_pairs(response.json(), "base"))
            except Exception:
                continue

        rows = []
        for token, pair in best.items():
            row = _base_score(pair, observed_at)
            sources = sorted(candidates.get(token, set()))
            row["discovery_feeds"] = ",".join(sources)
            row["discovery_feed_count"] = len(sources)
            rows.append(row)

        frame = pd.DataFrame(rows)
        _atomic_json(
            cache_path,
            {
                "checked_epoch": now,
                "checked_at": observed_at,
                "source_counts": source_counts,
                "rows": rows,
            },
        )

    if frame.empty:
        frame.to_csv(out_path, index=False)
        pd.DataFrame().to_csv(shortlist_path, index=False)
        pd.DataFrame().to_csv(wallet_path, index=False)
        return {
            "status": "DONE_EMPTY",
            "cached": cached,
            "tokens_seen": 0,
            "shortlisted": 0,
            "wallets": 0,
            "http_calls": http_calls,
            "source_counts": source_counts,
        }

    frame = frame.sort_values(
        ["radar_score", "discovery_feed_count"],
        ascending=[False, False],
        kind="mergesort",
    ).reset_index(drop=True)
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
        "source_counts": source_counts,
        "wallet_discovery_status": wallets.get("status"),
        "output": str(out_path),
        "shortlist_output": str(shortlist_path),
        "wallet_output": str(wallet_path),
    }
