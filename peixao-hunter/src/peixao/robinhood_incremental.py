from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import math
import os
import time
from urllib.parse import urlparse

import pandas as pd

from .evm_radar import ZERO_ADDRESS, _address, run_robinhood_radar
from .robinhood_chainstack import (
    ROBINHOOD_CHAIN_ID,
    RpcCallError,
    _is_eoa_paced,
    _paced_rpc,
    _topic_address,
)

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _provider_label(url: str | None) -> str:
    try:
        host = (urlparse(str(url or "")).hostname or "").lower()
    except Exception:
        host = ""
    if "drpc" in host:
        return "DRPC"
    if "chainstack" in host:
        return "CHAINSTACK"
    if "quiknode" in host or "quicknode" in host:
        return "QUICKNODE"
    if "alchemy" in host:
        return "ALCHEMY"
    return "ROBINHOOD_RPC"


def _rpc_head(
    url: str,
    *,
    timeout: float,
    pacing_state: dict,
    min_interval: float,
) -> tuple[int | None, int | None, int, str]:
    calls = 0
    try:
        chain_body, used = _paced_rpc(
            url,
            "eth_chainId",
            [],
            timeout,
            pacing_state=pacing_state,
            min_interval=min_interval,
        )
        calls += used
        chain_id = int(str(chain_body.get("result", "0x0")), 16)
        block_body, used = _paced_rpc(
            url,
            "eth_blockNumber",
            [],
            timeout,
            pacing_state=pacing_state,
            min_interval=min_interval,
        )
        calls += used
        latest = int(str(block_body.get("result", "0x0")), 16)
        return chain_id, latest, calls, ""
    except RpcCallError as exc:
        calls += max(1, int(getattr(exc, "attempts", 1)))
        return None, None, calls, str(exc)[:350]


def _shrinkable(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        hint in text
        for hint in (
            "range",
            "limit",
            "too many",
            "response body",
            "timeout",
            "timed out",
            "413",
            "more than",
        )
    )


def _fetch_transfer_logs_incremental(
    rpc_url: str,
    token: str,
    latest_block: int,
    *,
    timeout: float,
    lookback_blocks: int,
    chunk_blocks: int,
    max_logs: int,
    pacing_state: dict,
    min_interval: float,
) -> tuple[list[dict], int, int, dict]:
    """Scan newest->oldest and keep a provider-safe chunk once a cap is found."""
    floor = max(0, int(latest_block) - max(1, int(lookback_blocks)) + 1)
    end = int(latest_block)
    requested = max(10, int(chunk_blocks))
    try:
        host = (urlparse(str(rpc_url or "")).hostname or "").lower()
    except Exception:
        host = ""
    if "drpc" in host:
        requested = min(requested, 9500)

    target_chunk = requested
    chunk = target_chunk
    smallest_chunk = target_chunk
    logs: list[dict] = []
    calls = 0
    errors = 0
    rate_limit_errors = 0
    recovered_range_errors = 0
    consecutive_errors = 0
    first_error = ""
    fatal_error = ""

    while end >= floor and len(logs) < max(1, int(max_logs)):
        start = max(floor, end - chunk + 1)
        try:
            body, used = _paced_rpc(
                rpc_url,
                "eth_getLogs",
                [{
                    "fromBlock": hex(start),
                    "toBlock": hex(end),
                    "address": token,
                    "topics": [TRANSFER_TOPIC],
                }],
                timeout,
                pacing_state=pacing_state,
                min_interval=min_interval,
            )
            calls += used
            rows = body.get("result") if isinstance(body.get("result"), list) else []
            remaining = max(0, int(max_logs) - len(logs))
            if remaining:
                logs.extend([x for x in rows if isinstance(x, dict)][-remaining:])
            end = start - 1
            consecutive_errors = 0
            if chunk < target_chunk:
                chunk = min(target_chunk, max(chunk + 1, chunk * 2))
        except RpcCallError as exc:
            calls += max(1, int(getattr(exc, "attempts", 1)))
            errors += 1
            consecutive_errors += 1
            status = getattr(exc, "status_code", None)
            if status == 429:
                rate_limit_errors += 1
            if not first_error:
                first_error = str(exc)[:350]

            if _shrinkable(exc) and chunk > 10:
                new_chunk = max(10, chunk // 2)
                target_chunk = min(target_chunk, new_chunk)
                chunk = new_chunk
                smallest_chunk = min(smallest_chunk, new_chunk)
                recovered_range_errors += 1
                consecutive_errors = 0
                continue

            fatal_error = str(exc)[:350]
            if consecutive_errors >= 3 or status == 429:
                break
            end = start - 1

    stopped_on_log_cap = len(logs) >= max(1, int(max_logs))
    exhausted_requested_range = end < floor
    scan_complete = bool((exhausted_requested_range or stopped_on_log_cap) and not fatal_error)
    return logs, calls, errors, {
        "first_error": first_error,
        "fatal_error": fatal_error,
        "rate_limit_errors": int(rate_limit_errors),
        "smallest_chunk": int(smallest_chunk),
        "effective_chunk": int(target_chunk),
        "recovered_range_errors": int(recovered_range_errors),
        "scan_complete": bool(scan_complete),
        "stopped_on_log_cap": bool(stopped_on_log_cap),
        "range_floor": int(floor),
        "next_unscanned_end": int(end),
    }


def discover_wallets_incremental(
    shortlist: pd.DataFrame,
    *,
    rpc_url: str | None,
    output_path: Path,
    state_path: Path,
    provider_label: str,
    timeout: float = 15.0,
    min_cross_token_hits: int = 2,
    max_wallets: int = 75,
    bootstrap_lookback_blocks: int = 150_000,
    chunk_blocks: int = 9_500,
    max_logs_per_token: int = 1200,
    max_rps: float = 15.0,
    wallet_ttl_seconds: int = 7 * 86400,
    eoa_ttl_seconds: int = 86400,
) -> dict:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    if shortlist.empty or "token_address" not in shortlist.columns:
        pd.DataFrame().to_csv(output_path, index=False)
        return {"status": "DONE_EMPTY", "wallets": 0, "rpc_calls": 0, "tokens": 0, "provider": provider_label}

    url = str(rpc_url or "").strip()
    if not url:
        pd.DataFrame().to_csv(output_path, index=False)
        return {"status": "NOT_CONFIGURED_RPC", "wallets": 0, "rpc_calls": 0, "tokens": int(len(shortlist)), "provider": provider_label}

    state = _load_json(state_path)
    token_state = state.get("tokens") if isinstance(state.get("tokens"), dict) else {}
    wallet_state = state.get("wallets") if isinstance(state.get("wallets"), dict) else {}
    now = int(time.time())
    now_iso = _now_iso()
    pacing_state = {"last_request": 0.0}
    min_interval = 1.0 / max(1.0, float(max_rps))

    chain_id, latest_block, calls, head_error = _rpc_head(
        url,
        timeout=timeout,
        pacing_state=pacing_state,
        min_interval=min_interval,
    )
    if chain_id is None or latest_block is None:
        pd.DataFrame().to_csv(output_path, index=False)
        return {
            "status": "RPC_UNAVAILABLE",
            "wallets": 0,
            "rpc_calls": int(calls),
            "tokens": int(len(shortlist)),
            "provider": provider_label,
            "error": head_error,
        }
    if chain_id != ROBINHOOD_CHAIN_ID:
        pd.DataFrame().to_csv(output_path, index=False)
        return {
            "status": "WRONG_CHAIN",
            "wallets": 0,
            "rpc_calls": int(calls),
            "tokens": int(len(shortlist)),
            "provider": provider_label,
            "chain_id": int(chain_id),
        }

    tokens = [_address(x) for x in shortlist["token_address"].astype(str).tolist()]
    tokens = [x for x in tokens if x]
    token_universe = set(tokens)

    transfer_errors = 0
    rate_limit_errors = 0
    recovered_range_errors = 0
    first_error = ""
    new_logs = 0
    tokens_scanned = 0
    tokens_advanced = 0
    bootstrap_tokens = 0
    truncated_tokens = 0
    incomplete_tokens = 0
    delta_blocks_total = 0
    smallest_chunk = max(10, int(chunk_blocks))

    for token in tokens:
        previous = token_state.get(token) if isinstance(token_state.get(token), dict) else {}
        previous_block = int(previous.get("last_scanned_block", 0) or 0)
        if previous_block >= latest_block:
            continue
        if previous_block > 0:
            lookback = max(1, int(latest_block) - previous_block)
        else:
            lookback = max(1, int(bootstrap_lookback_blocks))
            bootstrap_tokens += 1
        delta_blocks_total += int(lookback)

        logs, used, errors, diagnostics = _fetch_transfer_logs_incremental(
            url,
            token,
            latest_block,
            timeout=timeout,
            lookback_blocks=lookback,
            chunk_blocks=chunk_blocks,
            max_logs=max_logs_per_token,
            pacing_state=pacing_state,
            min_interval=min_interval,
        )
        calls += used
        tokens_scanned += 1
        transfer_errors += int(errors)
        rate_limit_errors += int(diagnostics.get("rate_limit_errors", 0) or 0)
        recovered_range_errors += int(diagnostics.get("recovered_range_errors", 0) or 0)
        smallest_chunk = min(
            smallest_chunk,
            int(diagnostics.get("smallest_chunk", smallest_chunk) or smallest_chunk),
        )
        if not first_error and diagnostics.get("first_error"):
            first_error = str(diagnostics.get("first_error"))[:350]
        new_logs += len(logs)

        for event in logs:
            topics = event.get("topics") if isinstance(event.get("topics"), list) else []
            if len(topics) < 3:
                continue
            for topic in (topics[1], topics[2]):
                wallet = _topic_address(topic)
                if not wallet or wallet == ZERO_ADDRESS or wallet in token_universe:
                    continue
                item = wallet_state.get(wallet) if isinstance(wallet_state.get(wallet), dict) else {}
                token_counts = item.get("token_counts") if isinstance(item.get("token_counts"), dict) else {}
                token_counts[token] = int(token_counts.get(token, 0) or 0) + 1
                wallet_state[wallet] = {
                    **item,
                    "token_counts": token_counts,
                    "total_events": int(item.get("total_events", 0) or 0) + 1,
                    "first_seen_epoch": int(item.get("first_seen_epoch", now) or now),
                    "first_seen_at": str(item.get("first_seen_at", now_iso) or now_iso),
                    "last_seen_epoch": now,
                    "last_seen_at": now_iso,
                }

        if bool(diagnostics.get("scan_complete")):
            token_state[token] = {
                "last_scanned_block": int(latest_block),
                "updated_epoch": now,
                "updated_at": now_iso,
                "history_truncated_by_log_cap": bool(diagnostics.get("stopped_on_log_cap")),
                "effective_chunk": int(diagnostics.get("effective_chunk", chunk_blocks) or chunk_blocks),
            }
            tokens_advanced += 1
            if diagnostics.get("stopped_on_log_cap"):
                truncated_tokens += 1
            _atomic_json(
                state_path,
                {
                    "provider": provider_label,
                    "updated_epoch": now,
                    "updated_at": now_iso,
                    "latest_block": int(latest_block),
                    "tokens": token_state,
                    "wallets": wallet_state,
                },
            )
        else:
            incomplete_tokens += 1

    cutoff = now - max(3600, int(wallet_ttl_seconds))
    wallet_state = {
        wallet: item
        for wallet, item in wallet_state.items()
        if isinstance(item, dict) and int(item.get("last_seen_epoch", 0) or 0) >= cutoff
    }

    ranked: list[tuple[str, int, int]] = []
    for wallet, item in wallet_state.items():
        counts = item.get("token_counts") if isinstance(item.get("token_counts"), dict) else {}
        hits = len([token for token, count in counts.items() if int(count or 0) > 0])
        events = int(item.get("total_events", 0) or 0)
        if hits >= max(1, int(min_cross_token_hits)):
            ranked.append((wallet, hits, events))
    ranked.sort(key=lambda item: (item[1], item[2]), reverse=True)
    ranked = ranked[: max(1, int(max_wallets)) * 2]

    rows: list[dict] = []
    eoa_unknown = 0
    for wallet, hits, events in ranked:
        if len(rows) >= max(0, int(max_wallets)):
            break
        item = wallet_state.get(wallet) if isinstance(wallet_state.get(wallet), dict) else {}
        eoa_checked_epoch = int(item.get("eoa_checked_epoch", 0) or 0)
        is_eoa = item.get("is_eoa") if now - eoa_checked_epoch < max(0, int(eoa_ttl_seconds)) else None
        if is_eoa is None:
            is_eoa, used = _is_eoa_paced(
                url,
                wallet,
                timeout,
                pacing_state=pacing_state,
                min_interval=min_interval,
            )
            calls += used
            if is_eoa is None:
                # Falha de RPC: tenta de novo no próximo ciclo, sem cachear.
                eoa_unknown += 1
                continue
            item["is_eoa"] = bool(is_eoa)
            item["eoa_checked_epoch"] = now
            item["eoa_checked_at"] = now_iso
            wallet_state[wallet] = item
        if not bool(is_eoa):
            continue
        rows.append({
            "address": wallet,
            "chain": "robinhood",
            "chain_id": ROBINHOOD_CHAIN_ID,
            "discovery_source": f"{provider_label}_INCREMENTAL_TRANSFER_LOGS",
            "independent_cross_token_hits": int(hits),
            "distinct_tokens": int(hits),
            "total_transfer_events": int(events),
            "discovery_score": round(min(100.0, 35.0 + 15.0 * hits + 2.0 * math.log1p(events)), 2),
            "classification": "EOA_NO_CODE",
            "wallet_evidence_state": "DISCOVERY_ONLY",
            "incremental_last_seen_at": item.get("last_seen_at"),
        })

    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame = frame.sort_values(
            ["independent_cross_token_hits", "total_transfer_events", "discovery_score"],
            ascending=[False, False, False],
            kind="mergesort",
        ).reset_index(drop=True)
    frame.to_csv(output_path, index=False)

    _atomic_json(
        state_path,
        {
            "provider": provider_label,
            "updated_epoch": now,
            "updated_at": now_iso,
            "latest_block": int(latest_block),
            "tokens": token_state,
            "wallets": wallet_state,
        },
    )

    if tokens_scanned == 0:
        status = "DONE_NO_NEW_BLOCKS"
    elif incomplete_tokens:
        status = "PARTIAL"
    else:
        status = "DONE"

    return {
        "status": status,
        "wallets": int(len(frame)),
        "rpc_calls": int(calls),
        "tokens": int(len(tokens)),
        "tokens_scanned": int(tokens_scanned),
        "tokens_advanced": int(tokens_advanced),
        "bootstrap_tokens": int(bootstrap_tokens),
        "new_logs": int(new_logs),
        "delta_blocks_total": int(delta_blocks_total),
        "transfer_errors": int(transfer_errors),
        "recovered_range_errors": int(recovered_range_errors),
        "incomplete_tokens": int(incomplete_tokens),
        "truncated_tokens": int(truncated_tokens),
        "rate_limit_errors": int(rate_limit_errors),
        "eoa_unknown": int(eoa_unknown),
        "provider": provider_label,
        "latest_block": int(latest_block),
        "bootstrap_lookback_blocks": int(bootstrap_lookback_blocks),
        "chunk_blocks": int(chunk_blocks),
        "smallest_chunk": int(smallest_chunk),
        "max_rps": float(max_rps),
        "first_error": first_error,
        "output": str(output_path),
    }


def run_robinhood_radar_incremental(
    output_dir: Path,
    state_dir: Path,
    *,
    robinhood_rpc_url: str | None = None,
    public_rpc_url: str | None = None,
    alchemy_api_key: str | None = None,
    assets_base_url: str = "https://api.robinhood.com/rhj",
    timeout: float = 15.0,
    ttl_seconds: int = 600,
    max_candidates: int = 30,
    max_shortlist: int = 5,
    min_cross_token_hits: int = 2,
    max_wallets: int = 75,
    lookback_blocks: int = 150_000,
    chunk_blocks: int = 9_500,
    max_logs_per_token: int = 1200,
    max_rps: float = 15.0,
    prefer_free_rpc: bool = False,
) -> dict:
    """Descoberta de wallets Robinhood por logs de Transfer.

    Ordem: RPC primário configurado -> Alchemy (pago) -> RPC público (grátis).
    Com ``prefer_free_rpc`` (perfil economy) o RPC público incremental vem
    antes da Alchemy, e a Alchemy só é usada se o público falhar.
    """
    base = run_robinhood_radar(
        output_dir,
        state_dir,
        alchemy_api_key=None,
        assets_base_url=assets_base_url,
        timeout=timeout,
        ttl_seconds=ttl_seconds,
        max_candidates=max_candidates,
        max_shortlist=max_shortlist,
        min_cross_token_hits=min_cross_token_hits,
        max_wallets=max_wallets,
    )
    shortlist_path = output_dir / "V22_robinhood_token_radar_shortlist.csv"
    wallet_path = output_dir / "V22_robinhood_wallet_candidates.csv"
    try:
        shortlist = pd.read_csv(shortlist_path)
    except Exception:
        shortlist = pd.DataFrame()

    attempts: list[dict] = []
    primary_url = str(robinhood_rpc_url or "").strip()
    if primary_url:
        label = _provider_label(primary_url)
        primary = discover_wallets_incremental(
            shortlist,
            rpc_url=primary_url,
            output_path=wallet_path,
            state_path=state_dir / f"robinhood_incremental_{label.lower()}.json",
            provider_label=label,
            timeout=timeout,
            min_cross_token_hits=min_cross_token_hits,
            max_wallets=max_wallets,
            bootstrap_lookback_blocks=lookback_blocks,
            chunk_blocks=chunk_blocks,
            max_logs_per_token=max_logs_per_token,
            max_rps=max_rps,
        )
        attempts.append(primary)
        if int(primary.get("wallets", 0)) > 0 or primary.get("status") in {"DONE", "DONE_NO_NEW_BLOCKS"}:
            return {
                **base,
                "status": "DONE" if primary.get("status") in {"DONE", "DONE_NO_NEW_BLOCKS"} else "PARTIAL",
                "wallets": int(primary.get("wallets", 0)),
                "http_calls": int(base.get("http_calls", 0)),
                "rpc_calls": int(primary.get("rpc_calls", 0)),
                "wallet_discovery_status": primary.get("status"),
                "wallet_discovery_provider": label,
                "provider_attempts": attempts,
                "incremental": True,
            }

    fallback_url = str(public_rpc_url or "").strip()

    def _public_attempt(*, accept_empty: bool) -> dict | None:
        if not fallback_url or fallback_url == primary_url:
            return None
        fallback = discover_wallets_incremental(
            shortlist,
            rpc_url=fallback_url,
            output_path=wallet_path,
            state_path=state_dir / "robinhood_incremental_public.json",
            provider_label="ROBINHOOD_PUBLIC",
            timeout=timeout,
            min_cross_token_hits=min_cross_token_hits,
            max_wallets=max_wallets,
            bootstrap_lookback_blocks=min(lookback_blocks, 20_000),
            chunk_blocks=min(chunk_blocks, 2_000),
            max_logs_per_token=max_logs_per_token,
            max_rps=min(max_rps, 5.0),
        )
        attempts.append(fallback)
        succeeded = fallback.get("status") in {"DONE", "DONE_NO_NEW_BLOCKS"}
        if int(fallback.get("wallets", 0)) > 0 or (accept_empty and succeeded):
            return {
                **base,
                "status": "DONE" if succeeded else "PARTIAL",
                "wallets": int(fallback.get("wallets", 0)),
                "http_calls": int(base.get("http_calls", 0)),
                "rpc_calls": int(fallback.get("rpc_calls", 0)),
                "wallet_discovery_status": fallback.get("status"),
                "wallet_discovery_provider": fallback.get("provider"),
                "provider_attempts": attempts,
                "incremental": True,
            }
        return None

    public_tried = False
    if prefer_free_rpc:
        public_tried = bool(fallback_url and fallback_url != primary_url)
        free_result = _public_attempt(accept_empty=True)
        if free_result is not None:
            return free_result

    key = str(alchemy_api_key or "").strip()
    if key:
        alchemy = run_robinhood_radar(
            output_dir,
            state_dir,
            alchemy_api_key=key,
            assets_base_url=assets_base_url,
            timeout=timeout,
            ttl_seconds=ttl_seconds,
            max_candidates=max_candidates,
            max_shortlist=max_shortlist,
            min_cross_token_hits=min_cross_token_hits,
            max_wallets=max_wallets,
        )
        attempts.append({
            "status": alchemy.get("wallet_discovery_status"),
            "wallets": int(alchemy.get("wallets", 0)),
            "provider": "ALCHEMY",
            "rpc_calls": max(0, int(alchemy.get("http_calls", 0)) - int(base.get("http_calls", 0))),
        })
        if int(alchemy.get("wallets", 0)) > 0:
            return {
                **alchemy,
                "wallet_discovery_provider": "ALCHEMY",
                "provider_attempts": attempts,
                "incremental": False,
            }

    if not public_tried:
        free_result = _public_attempt(accept_empty=False)
        if free_result is not None:
            return free_result

    last = attempts[-1] if attempts else {
        "status": "NOT_CONFIGURED_RPC",
        "wallets": 0,
        "provider": None,
        "rpc_calls": 0,
    }
    return {
        **base,
        "status": "DONE" if last.get("status") in {"DONE", "DONE_EMPTY", "DONE_NO_NEW_BLOCKS", "PARTIAL"} else "PARTIAL",
        "wallets": 0,
        "http_calls": int(base.get("http_calls", 0)),
        "rpc_calls": int(sum(int(x.get("rpc_calls", 0)) for x in attempts)),
        "wallet_discovery_status": last.get("status"),
        "wallet_discovery_provider": last.get("provider"),
        "provider_attempts": attempts,
        "incremental": True,
    }
