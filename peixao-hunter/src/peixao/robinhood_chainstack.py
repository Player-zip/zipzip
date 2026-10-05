from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import math
import time

import pandas as pd
import requests

from .evm_radar import (
    ZERO_ADDRESS,
    _address,
    run_robinhood_radar,
)


TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ROBINHOOD_CHAIN_ID = 4663


class RpcCallError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, attempts: int = 1):
        super().__init__(message)
        self.status_code = status_code
        self.attempts = attempts


def _topic_address(value) -> str:
    text = str(value or "").strip().lower()
    if not text.startswith("0x") or len(text) < 42:
        return ""
    return _address("0x" + text[-40:])


def _paced_rpc(
    url: str,
    method: str,
    params: list,
    timeout: float,
    *,
    pacing_state: dict,
    min_interval: float,
    max_retries: int = 4,
) -> tuple[dict, int]:
    """JSON-RPC call with a small provider-friendly RPS cap and 429 backoff."""
    attempts = 0
    last_error: Exception | None = None

    for retry in range(max(0, int(max_retries)) + 1):
        wait = float(min_interval) - (time.monotonic() - float(pacing_state.get("last_request", 0.0)))
        if wait > 0:
            time.sleep(wait)

        attempts += 1
        pacing_state["last_request"] = time.monotonic()
        try:
            response = requests.post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=float(timeout),
            )
            status = int(response.status_code)
            if status == 429:
                if retry < max_retries:
                    time.sleep(min(8.0, 0.75 * (2 ** retry)))
                    continue
                raise RpcCallError("HTTP 429 rate limited", status_code=429, attempts=attempts)
            response.raise_for_status()
            body = response.json()
            if isinstance(body, dict) and body.get("error"):
                raise RpcCallError(str(body.get("error")), status_code=status, attempts=attempts)
            return (body if isinstance(body, dict) else {}), attempts
        except RpcCallError as exc:
            last_error = exc
            if exc.status_code == 429 and retry < max_retries:
                time.sleep(min(8.0, 0.75 * (2 ** retry)))
                continue
            raise
        except requests.HTTPError as exc:
            status = int(exc.response.status_code) if exc.response is not None else None
            last_error = exc
            if status == 429 and retry < max_retries:
                time.sleep(min(8.0, 0.75 * (2 ** retry)))
                continue
            detail = ""
            try:
                detail = str(exc.response.text or "")[:300] if exc.response is not None else ""
            except Exception:
                detail = ""
            raise RpcCallError(
                f"HTTP {status or 'error'} {detail}".strip(),
                status_code=status,
                attempts=attempts,
            ) from exc
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if retry < max_retries:
                time.sleep(min(4.0, 0.35 * (2 ** retry)))
                continue
            raise RpcCallError(f"{type(exc).__name__}: {exc}", attempts=attempts) from exc

    raise RpcCallError(f"RPC failed: {last_error}", attempts=attempts)


def _should_shrink_range(exc: Exception) -> bool:
    text = str(exc).lower()
    hints = (
        "limit",
        "range",
        "too many",
        "response body",
        "missing or invalid parameters",
        "timeout",
        "timed out",
        "413",
    )
    return any(hint in text for hint in hints)


def _fetch_transfer_logs(
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
    floor = max(0, int(latest_block) - max(1, int(lookback_blocks)) + 1)
    end = int(latest_block)
    target_chunk = max(10, int(chunk_blocks))
    chunk = target_chunk
    logs: list[dict] = []
    calls = 0
    errors = 0
    consecutive_errors = 0
    first_error = ""
    rate_limit_errors = 0
    smallest_chunk = target_chunk

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
                chunk = min(target_chunk, chunk * 2)
        except RpcCallError as exc:
            calls += max(1, int(getattr(exc, "attempts", 1)))
            errors += 1
            consecutive_errors += 1
            if getattr(exc, "status_code", None) == 429:
                rate_limit_errors += 1
            if not first_error:
                first_error = str(exc)[:350]

            # Provider/query caps should reduce the block span. A pure rate-limit
            # error is retried above; if it survives all retries, stop this token
            # rather than hammering the endpoint with hundreds of doomed calls.
            if _should_shrink_range(exc) and chunk > 10:
                chunk = max(10, chunk // 2)
                smallest_chunk = min(smallest_chunk, chunk)
                continue
            if consecutive_errors >= 3 or getattr(exc, "status_code", None) == 429:
                break
            end = start - 1

    return logs, calls, errors, {
        "first_error": first_error,
        "rate_limit_errors": int(rate_limit_errors),
        "smallest_chunk": int(smallest_chunk),
    }


def _is_eoa_paced(
    rpc_url: str,
    address: str,
    timeout: float,
    *,
    pacing_state: dict,
    min_interval: float,
) -> tuple[bool, int]:
    try:
        body, used = _paced_rpc(
            rpc_url,
            "eth_getCode",
            [address, "latest"],
            timeout,
            pacing_state=pacing_state,
            min_interval=min_interval,
        )
        code = str(body.get("result") or "").lower()
        return code in {"", "0x", "0x0", "0x00"}, used
    except RpcCallError as exc:
        return False, max(1, int(getattr(exc, "attempts", 1)))


def discover_wallets_from_tokens_rpc(
    shortlist: pd.DataFrame,
    *,
    rpc_url: str | None,
    output_path: Path,
    provider_label: str,
    timeout: float = 15.0,
    min_cross_token_hits: int = 2,
    max_wallets: int = 50,
    lookback_blocks: int = 150_000,
    chunk_blocks: int = 2_000,
    max_logs_per_token: int = 800,
    max_rps: float = 15.0,
) -> dict:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if shortlist.empty or "token_address" not in shortlist.columns:
        pd.DataFrame().to_csv(output_path, index=False)
        return {"status": "DONE_EMPTY", "wallets": 0, "rpc_calls": 0, "tokens": 0, "provider": provider_label}

    url = str(rpc_url or "").strip()
    if not url:
        pd.DataFrame().to_csv(output_path, index=False)
        return {
            "status": "NOT_CONFIGURED_RPC",
            "wallets": 0,
            "rpc_calls": 0,
            "tokens": int(len(shortlist)),
            "provider": provider_label,
        }

    calls = 0
    pacing_state = {"last_request": 0.0}
    min_interval = 1.0 / max(1.0, float(max_rps))
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
        if chain_id != ROBINHOOD_CHAIN_ID:
            pd.DataFrame().to_csv(output_path, index=False)
            return {
                "status": "WRONG_CHAIN",
                "wallets": 0,
                "rpc_calls": calls,
                "tokens": int(len(shortlist)),
                "provider": provider_label,
                "chain_id": chain_id,
            }
        block_body, used = _paced_rpc(
            url,
            "eth_blockNumber",
            [],
            timeout,
            pacing_state=pacing_state,
            min_interval=min_interval,
        )
        calls += used
        latest_block = int(str(block_body.get("result", "0x0")), 16)
    except RpcCallError as exc:
        calls += max(1, int(getattr(exc, "attempts", 1)))
        pd.DataFrame().to_csv(output_path, index=False)
        return {
            "status": "RPC_UNAVAILABLE",
            "wallets": 0,
            "rpc_calls": calls,
            "tokens": int(len(shortlist)),
            "provider": provider_label,
            "error": str(exc)[:350],
        }

    tokens = [_address(x) for x in shortlist["token_address"].astype(str).tolist()]
    tokens = [x for x in tokens if x]
    token_universe = set(tokens)
    token_sets: dict[str, set[str]] = defaultdict(set)
    event_counts: dict[str, int] = defaultdict(int)
    transfer_errors = 0
    token_successes = 0
    rate_limit_errors = 0
    first_error = ""
    smallest_chunk = max(10, int(chunk_blocks))

    for token in tokens:
        logs, used, errors, diagnostics = _fetch_transfer_logs(
            url,
            token,
            latest_block,
            timeout=timeout,
            lookback_blocks=lookback_blocks,
            chunk_blocks=chunk_blocks,
            max_logs=max_logs_per_token,
            pacing_state=pacing_state,
            min_interval=min_interval,
        )
        calls += used
        transfer_errors += errors
        rate_limit_errors += int(diagnostics.get("rate_limit_errors", 0))
        smallest_chunk = min(smallest_chunk, int(diagnostics.get("smallest_chunk", smallest_chunk)))
        if not first_error and diagnostics.get("first_error"):
            first_error = str(diagnostics.get("first_error"))[:350]
        if logs:
            token_successes += 1
        for event in logs:
            topics = event.get("topics") if isinstance(event.get("topics"), list) else []
            if len(topics) < 3:
                continue
            for topic in (topics[1], topics[2]):
                wallet = _topic_address(topic)
                if not wallet or wallet == ZERO_ADDRESS or wallet in token_universe:
                    continue
                token_sets[wallet].add(token)
                event_counts[wallet] += 1

    ranked = sorted(
        token_sets,
        key=lambda wallet: (len(token_sets[wallet]), event_counts[wallet]),
        reverse=True,
    )
    ranked = [w for w in ranked if len(token_sets[w]) >= max(1, int(min_cross_token_hits))]
    ranked = ranked[: max(1, int(max_wallets)) * 5]

    rows = []
    for wallet in ranked:
        if len(rows) >= max(0, int(max_wallets)):
            break
        is_eoa, used = _is_eoa_paced(
            url,
            wallet,
            timeout,
            pacing_state=pacing_state,
            min_interval=min_interval,
        )
        calls += used
        if not is_eoa:
            continue
        hits = len(token_sets[wallet])
        events = event_counts[wallet]
        rows.append({
            "address": wallet,
            "chain": "robinhood",
            "chain_id": ROBINHOOD_CHAIN_ID,
            "discovery_source": f"{provider_label}_TRANSFER_LOGS",
            "independent_cross_token_hits": int(hits),
            "distinct_tokens": int(hits),
            "total_transfer_events": int(events),
            "discovery_score": round(min(100.0, 35.0 + 15.0 * hits + 2.0 * math.log1p(events)), 2),
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

    if token_successes == 0 and transfer_errors:
        status = "RPC_LOGS_UNAVAILABLE"
    elif transfer_errors:
        status = "PARTIAL"
    else:
        status = "DONE"

    return {
        "status": status,
        "wallets": int(len(frame)),
        "rpc_calls": int(calls),
        "tokens": int(len(tokens)),
        "tokens_with_logs": int(token_successes),
        "transfer_errors": int(transfer_errors),
        "rate_limit_errors": int(rate_limit_errors),
        "provider": provider_label,
        "latest_block": int(latest_block),
        "lookback_blocks": int(lookback_blocks),
        "chunk_blocks": int(chunk_blocks),
        "smallest_chunk": int(smallest_chunk),
        "max_rps": float(max_rps),
        "first_error": first_error,
        "output": str(output_path),
    }


def run_robinhood_radar_with_rpc(
    output_dir: Path,
    state_dir: Path,
    *,
    robinhood_rpc_url: str | None = None,
    public_rpc_url: str | None = None,
    alchemy_api_key: str | None = None,
    assets_base_url: str = "https://api.robinhood.com/rhj",
    timeout: float = 15.0,
    ttl_seconds: int = 1800,
    max_candidates: int = 30,
    max_shortlist: int = 5,
    min_cross_token_hits: int = 2,
    max_wallets: int = 50,
    lookback_blocks: int = 150_000,
    chunk_blocks: int = 2_000,
    max_logs_per_token: int = 800,
    max_rps: float = 15.0,
) -> dict:
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
        primary = discover_wallets_from_tokens_rpc(
            shortlist,
            rpc_url=primary_url,
            output_path=wallet_path,
            provider_label="CHAINSTACK",
            timeout=timeout,
            min_cross_token_hits=min_cross_token_hits,
            max_wallets=max_wallets,
            lookback_blocks=lookback_blocks,
            chunk_blocks=chunk_blocks,
            max_logs_per_token=max_logs_per_token,
            max_rps=max_rps,
        )
        attempts.append(primary)
        if int(primary.get("wallets", 0)) > 0:
            return {
                **base,
                "status": "DONE" if primary.get("status") in {"DONE", "PARTIAL"} else "PARTIAL",
                "wallets": int(primary.get("wallets", 0)),
                "http_calls": int(base.get("http_calls", 0)),
                "rpc_calls": int(primary.get("rpc_calls", 0)),
                "wallet_discovery_status": primary.get("status"),
                "wallet_discovery_provider": primary.get("provider"),
                "provider_attempts": attempts,
            }

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
            }

    fallback_url = str(public_rpc_url or "").strip()
    if fallback_url and fallback_url != primary_url:
        fallback = discover_wallets_from_tokens_rpc(
            shortlist,
            rpc_url=fallback_url,
            output_path=wallet_path,
            provider_label="ROBINHOOD_PUBLIC",
            timeout=timeout,
            min_cross_token_hits=min_cross_token_hits,
            max_wallets=max_wallets,
            lookback_blocks=lookback_blocks,
            chunk_blocks=chunk_blocks,
            max_logs_per_token=max_logs_per_token,
            max_rps=max_rps,
        )
        attempts.append(fallback)
        if int(fallback.get("wallets", 0)) > 0:
            return {
                **base,
                "status": "DONE" if fallback.get("status") in {"DONE", "PARTIAL"} else "PARTIAL",
                "wallets": int(fallback.get("wallets", 0)),
                "http_calls": int(base.get("http_calls", 0)),
                "rpc_calls": int(fallback.get("rpc_calls", 0)),
                "wallet_discovery_status": fallback.get("status"),
                "wallet_discovery_provider": fallback.get("provider"),
                "provider_attempts": attempts,
            }

    last = attempts[-1] if attempts else {"status": "NOT_CONFIGURED_RPC", "wallets": 0, "provider": None, "rpc_calls": 0}
    return {
        **base,
        "status": "DONE" if last.get("status") in {"DONE", "DONE_EMPTY", "PARTIAL"} else "PARTIAL",
        "wallets": 0,
        "http_calls": int(base.get("http_calls", 0)),
        "rpc_calls": int(sum(int(x.get("rpc_calls", 0)) for x in attempts)),
        "wallet_discovery_status": last.get("status"),
        "wallet_discovery_provider": last.get("provider"),
        "provider_attempts": attempts,
    }
