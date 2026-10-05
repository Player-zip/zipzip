from __future__ import annotations

from pathlib import Path
import json
import os
import shutil
import time
import requests
import pandas as pd

from ..rpc_budget import RpcBudgetManager
from .discovery import norm_addr


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def seed_cache_if_missing(source_dir: Path | None, runtime_cache_dir: Path, names: tuple[str, ...]) -> list[str]:
    runtime_cache_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    if not source_dir or not source_dir.is_dir():
        return copied
    for name in names:
        src = source_dir / name
        dst = runtime_cache_dir / name
        if not dst.exists() and src.is_file():
            shutil.copy2(src, dst)
            copied.append(name)
    return copied


def _rpc_call(
    session: requests.Session,
    url: str,
    method: str,
    params: list,
    *,
    timeout: float,
    retries: int,
    delay: float,
    call_id: int,
    budget: RpcBudgetManager | None = None,
    budget_stage: str = "wallet_validation",
    provider: str = "primary_evm",
):
    """Execute an RPC request while charging every physical HTTP attempt.

    Retries are not free: the budget is reserved before each POST. If the
    budget is exhausted, the caller gets a special error and can stop without
    poisoning the persistent result cache with a fake provider failure.
    """
    payload = {"jsonrpc": "2.0", "id": call_id, "method": method, "params": params}
    last_error = None
    physical_attempts = 0
    for attempt in range(retries):
        if budget is not None and not budget.authorize(budget_stage, provider, method):
            return None, "RPC_BUDGET_EXHAUSTED", physical_attempts
        physical_attempts += 1
        try:
            response = session.post(url, json=payload, timeout=timeout)
            if response.status_code == 429:
                time.sleep(min(8.0, 0.7 * (2 ** attempt)))
                continue
            response.raise_for_status()
            data = response.json()
            if data.get("error"):
                last_error = data["error"]
                time.sleep(min(3.0, 0.4 * (attempt + 1)))
                continue
            if delay:
                time.sleep(delay)
            return data.get("result"), None, physical_attempts
        except Exception as exc:
            last_error = str(exc)
            time.sleep(min(4.0, 0.5 * (2 ** attempt)))
    return None, str(last_error), physical_attempts


def resolve_tx_origins(
    tx_sample: pd.DataFrame,
    runtime_cache_dir: Path,
    output_dir: Path,
    *,
    rpc_url: str,
    min_tokens: int = 2,
    max_rpc_tx: int = 1200,
    timeout: float = 20,
    retries: int = 4,
    delay: float = 0.08,
    run_rpc: bool = True,
    rpc_budget: RpcBudgetManager | None = None,
    rpc_provider: str = "primary_evm",
) -> dict:
    if not run_rpc:
        empty = pd.DataFrame()
        empty.to_csv(output_dir / "V6_tx_origins.csv", index=False)
        empty.to_csv(output_dir / "V6_origin_candidates.csv", index=False)
        return {"tx_origins": empty, "origin_candidates": empty, "rpc_calls": 0, "rpc_budget_denied": 0}

    cache_path = runtime_cache_dir / "tx_origin_cache.json"
    try:
        tx_cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        if not isinstance(tx_cache, dict):
            tx_cache = {}
    except Exception:
        tx_cache = {}

    todo = []
    seen = set()
    if not tx_sample.empty:
        for row in tx_sample.itertuples(index=False):
            tx_hash = str(row.tx_hash).lower()
            if tx_hash not in seen:
                seen.add(tx_hash)
                todo.append(tx_hash)

    cached_count = sum(1 for tx_hash in todo if tx_hash in tx_cache)
    if rpc_budget is not None:
        rpc_budget.note_cache_hit("tx_origin", cached_count)

    unresolved = [tx_hash for tx_hash in todo if tx_hash not in tx_cache][:max_rpc_tx]
    session = requests.Session()
    physical_calls = 0
    logical_calls = 0
    budget_denied = 0
    for n, tx_hash in enumerate(unresolved, 1):
        result, error, attempts = _rpc_call(
            session,
            rpc_url,
            "eth_getTransactionByHash",
            [tx_hash],
            timeout=timeout,
            retries=retries,
            delay=delay,
            call_id=n,
            budget=rpc_budget,
            budget_stage="tx_origin",
            provider=rpc_provider,
        )
        physical_calls += attempts
        if error == "RPC_BUDGET_EXHAUSTED":
            budget_denied += 1
            break
        logical_calls += 1
        if isinstance(result, dict):
            tx_cache[tx_hash] = {
                "from": norm_addr(result.get("from")),
                "to": norm_addr(result.get("to")),
                "blockNumber": result.get("blockNumber"),
                "status": "OK",
            }
        else:
            tx_cache[tx_hash] = {"from": None, "to": None, "status": "ERROR", "error": error or "null result"}
        if n % 25 == 0 or n == len(unresolved):
            _atomic_json(cache_path, tx_cache)
    _atomic_json(cache_path, tx_cache)

    rows = []
    if not tx_sample.empty:
        for row in tx_sample.itertuples(index=False):
            tx_hash = str(row.tx_hash).lower()
            info = tx_cache.get(tx_hash) or {}
            origin = norm_addr(info.get("from"))
            if not origin:
                continue
            rows.append({
                "origin": origin,
                "token": row.token,
                "symbol": row.symbol,
                "pool": row.pool,
                "block": row.block,
                "tx_hash": tx_hash,
                "tx_to": norm_addr(info.get("to")),
            })
    tx_origins = pd.DataFrame(rows)
    if not tx_origins.empty:
        join_sorted = lambda values: ", ".join(sorted(set(str(x) for x in values)))
        summary = tx_origins.groupby("origin", as_index=False).agg(
            distinct_tokens=("token", "nunique"),
            distinct_pools=("pool", "nunique"),
            sampled_txs=("tx_hash", "nunique"),
            symbols=("symbol", join_sorted),
            first_block=("block", "min"),
            last_block=("block", "max"),
        )
        origin_candidates = summary[summary["distinct_tokens"] >= min_tokens].sort_values(
            ["distinct_tokens", "sampled_txs"], ascending=[False, False]
        ).reset_index(drop=True)
    else:
        origin_candidates = pd.DataFrame()

    output_dir.mkdir(parents=True, exist_ok=True)
    tx_origins.to_csv(output_dir / "V6_tx_origins.csv", index=False)
    origin_candidates.to_csv(output_dir / "V6_origin_candidates.csv", index=False)
    return {
        "tx_origins": tx_origins,
        "origin_candidates": origin_candidates,
        "rpc_calls": int(physical_calls),
        "rpc_logical_calls": int(logical_calls),
        "rpc_cache_hits": int(cached_count),
        "rpc_budget_denied": int(budget_denied),
    }


def classify_candidates(
    origin_candidates: pd.DataFrame,
    actor_candidates: pd.DataFrame,
    runtime_cache_dir: Path,
    output_dir: Path,
    *,
    rpc_url: str,
    timeout: float = 20,
    retries: int = 4,
    delay: float = 0.08,
    run_rpc: bool = True,
    rpc_budget: RpcBudgetManager | None = None,
    rpc_provider: str = "primary_evm",
) -> dict[str, pd.DataFrame]:
    addresses = []
    if not origin_candidates.empty:
        addresses.extend(origin_candidates["origin"].astype(str).tolist())
    if not actor_candidates.empty:
        addresses.extend(actor_candidates["address"].astype(str).tolist())
    addresses = list(dict.fromkeys(address.lower() for address in addresses if address))

    cache_path = runtime_cache_dir / "address_code_cache.json"
    try:
        code_cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        if not isinstance(code_cache, dict):
            code_cache = {}
    except Exception:
        code_cache = {}

    pending = [address for address in addresses if address not in code_cache]
    cached_count = len(addresses) - len(pending)
    if rpc_budget is not None:
        rpc_budget.note_cache_hit("classification", cached_count)

    physical_calls = 0
    logical_calls = 0
    budget_denied = 0
    if run_rpc:
        session = requests.Session()
        for idx, address in enumerate(pending, 1):
            result, error, attempts = _rpc_call(
                session,
                rpc_url,
                "eth_getCode",
                [address, "latest"],
                timeout=timeout,
                retries=retries,
                delay=delay,
                call_id=idx,
                budget=rpc_budget,
                budget_stage="classification",
                provider=rpc_provider,
            )
            physical_calls += attempts
            if error == "RPC_BUDGET_EXHAUSTED":
                budget_denied += 1
                break
            logical_calls += 1
            if result is None:
                label = "UNKNOWN"
            elif str(result).lower() in ("0x", "0x0", ""):
                label = "EOA_NO_CODE"
            else:
                label = "CONTRACT"
            code_cache[address] = {
                "classification": label,
                "code_len": len(str(result)) if result is not None else None,
            }
            if idx % 25 == 0 or idx == len(pending):
                _atomic_json(cache_path, code_cache)
    _atomic_json(cache_path, code_cache)

    final_rows = []
    if not origin_candidates.empty:
        for row in origin_candidates.to_dict("records"):
            address = row["origin"]
            final_rows.append({
                "address": address,
                "source": "TX_FROM",
                "distinct_tokens": row["distinct_tokens"],
                "sampled_txs": row["sampled_txs"],
                "symbols": row["symbols"],
                "address_type": code_cache.get(address, {}).get("classification", "UNCLASSIFIED"),
            })
    origin_set = {str(row["address"]).lower() for row in final_rows}
    if not actor_candidates.empty:
        for row in actor_candidates.to_dict("records"):
            address = str(row["address"]).lower()
            if address in origin_set:
                continue
            final_rows.append({
                "address": address,
                "source": "SWAP_INDEXED_ACTOR",
                "distinct_tokens": row["distinct_tokens"],
                "sampled_txs": row["unique_txs"],
                "symbols": row["symbols"],
                "address_type": code_cache.get(address, {}).get("classification", "UNCLASSIFIED"),
            })
    final = pd.DataFrame(final_rows)
    if not final.empty:
        final = final.sort_values(
            ["distinct_tokens", "sampled_txs", "source"], ascending=[False, False, True]
        ).reset_index(drop=True)
        direct_eoa = final[
            final["source"].eq("TX_FROM") & final["address_type"].eq("EOA_NO_CODE")
        ].copy()
    else:
        direct_eoa = pd.DataFrame()

    output_dir.mkdir(parents=True, exist_ok=True)
    final.to_csv(output_dir / "V6_final_candidates.csv", index=False)
    direct_eoa.to_csv(output_dir / "V6_direct_eoa_candidates.csv", index=False)
    return {
        "final_candidates": final,
        "direct_eoa": direct_eoa,
        "rpc_calls": int(physical_calls),
        "rpc_logical_calls": int(logical_calls),
        "rpc_cache_hits": int(cached_count),
        "rpc_budget_denied": int(budget_denied),
    }
