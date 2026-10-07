from __future__ import annotations

from pathlib import Path
import json
import pandas as pd

V2_SWAP = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
V3_SWAP = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"


def norm_addr(value):
    if value is None:
        return None
    value = str(value).strip().lower()
    return value or None


def as_int(value):
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 16) if value.startswith("0x") else int(value)
    return int(value)


def topic0(log: dict):
    topics = log.get("topics") or []
    return str(topics[0]).lower() if topics else None


def indexed_address(log: dict, idx: int):
    topics = log.get("topics") or []
    if len(topics) <= idx:
        return None
    value = str(topics[idx])
    value = value[2:] if value.startswith("0x") else value
    if len(value) < 40:
        return None
    tail = value[-40:]
    try:
        int(tail, 16)
    except Exception:
        return None
    address = "0x" + tail.lower()
    if address == "0x" + "0" * 40:
        return None
    return address


def _checkpoint_block(result: dict, name: str):
    value = result.get(f"block_{name}")
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def find_v3b_json(project_root: Path) -> tuple[Path, dict]:
    preferred = [
        project_root / "V3B_CHECKPOINT.json",
        project_root / "V3B_CHECKPOINTS.json",
        project_root / "V3B_PRICE_IMPACT.json",
    ]
    candidates = preferred + sorted(project_root.glob("*.json"))
    seen: set[Path] = set()
    for path in candidates:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if (
            isinstance(data, dict)
            and isinstance(data.get("results"), dict)
            and isinstance(data.get("pool_static"), dict)
            and isinstance(data.get("token_decimals"), dict)
        ):
            return path, data
    raise RuntimeError("V3B JSON não localizado na raiz do projeto.")


def discover_offline(
    project_root: Path,
    cache_dir: Path,
    output_dir: Path,
    *,
    start_checkpoint: str = "entry",
    end_checkpoint: str = "p1h",
    min_tokens: int = 2,
    tx_per_token: int = 40,
) -> dict[str, pd.DataFrame]:
    """Port fiel da etapa 02 do V6 validado no Colab (zero RPC/API)."""
    _, v3b = find_v3b_json(project_root)
    results = v3b["results"]
    cache_index: dict[str, dict] = {}
    for path in sorted(cache_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        pool = norm_addr(data.get("pool"))
        if pool:
            cache_index[pool] = {"path": path, "data": data}

    targets = []
    for token, result in results.items():
        if not isinstance(result, dict):
            continue
        pool = norm_addr(result.get("pool"))
        if not pool or pool not in cache_index:
            continue
        targets.append({
            "token": norm_addr(token), "symbol": str(result.get("symbol", "?")), "pool": pool,
            "result": result, "cache_path": cache_index[pool]["path"], "cache": cache_index[pool]["data"],
        })
    targets.sort(key=lambda row: row["cache_path"].name)

    actor_rows, tx_rows, coverage_rows = [], [], []
    for target in targets:
        result = target["result"]
        logs = target["cache"].get("logs") or []
        start_block = _checkpoint_block(result, start_checkpoint)
        end_block = _checkpoint_block(result, end_checkpoint)
        if start_block is None:
            continue
        if end_block is None:
            for fallback in ("p6h", "p24h"):
                end_block = _checkpoint_block(result, fallback)
                if end_block is not None:
                    break
        if end_block is None:
            blocks = []
            for log in logs:
                try:
                    blocks.append(as_int(log.get("blockNumber")))
                except Exception:
                    pass
            end_block = max(blocks) if blocks else start_block
        if end_block < start_block:
            start_block, end_block = end_block, start_block

        swap_logs = 0
        window_logs = 0
        window_tx_hashes = []
        for log in logs:
            try:
                block = as_int(log.get("blockNumber"))
            except Exception:
                continue
            event = topic0(log)
            tx_hash = log.get("transactionHash")
            if start_block <= block <= end_block:
                window_logs += 1
                if tx_hash:
                    window_tx_hashes.append((block, str(tx_hash).lower()))
            if event not in (V2_SWAP, V3_SWAP):
                continue
            swap_logs += 1
            if not (start_block <= block <= end_block):
                continue
            sender = indexed_address(log, 1)
            recipient = indexed_address(log, 2)
            for role, address in (("sender", sender), ("recipient", recipient)):
                if address:
                    actor_rows.append({
                        "address": address, "role": role, "token": target["token"], "symbol": target["symbol"],
                        "pool": target["pool"], "block": block,
                        "tx_hash": str(tx_hash).lower() if tx_hash else None,
                    })

        unique = []
        seen_tx = set()
        for block, tx_hash in sorted(window_tx_hashes):
            if tx_hash in seen_tx:
                continue
            seen_tx.add(tx_hash)
            unique.append((block, tx_hash))
            if len(unique) >= tx_per_token:
                break
        for block, tx_hash in unique:
            tx_rows.append({"token": target["token"], "symbol": target["symbol"], "pool": target["pool"], "block": block, "tx_hash": tx_hash})
        coverage_rows.append({
            "symbol": target["symbol"], "token": target["token"], "pool": target["pool"],
            "start_block": start_block, "end_block": end_block, "cache_logs": len(logs),
            "swap_logs_total": swap_logs, "window_logs": window_logs, "sampled_txs": len(unique),
        })

    actor_events = pd.DataFrame(actor_rows)
    tx_sample = pd.DataFrame(tx_rows)
    coverage = pd.DataFrame(coverage_rows)
    if not actor_events.empty:
        join_sorted = lambda values: ", ".join(sorted(set(str(x) for x in values)))
        actor_summary = actor_events.groupby("address", as_index=False).agg(
            distinct_tokens=("token", "nunique"), distinct_pools=("pool", "nunique"),
            events=("tx_hash", "size"), unique_txs=("tx_hash", "nunique"),
            sender_events=("role", lambda s: int((s == "sender").sum())),
            recipient_events=("role", lambda s: int((s == "recipient").sum())),
            symbols=("symbol", join_sorted), first_block=("block", "min"), last_block=("block", "max"),
        )
        actor_candidates = actor_summary[actor_summary["distinct_tokens"] >= min_tokens].sort_values(
            ["distinct_tokens", "unique_txs", "events"], ascending=[False, False, False]
        ).reset_index(drop=True)
    else:
        actor_candidates = pd.DataFrame()

    output_dir.mkdir(parents=True, exist_ok=True)
    coverage.to_csv(output_dir / "V6_coverage.csv", index=False)
    tx_sample.to_csv(output_dir / "V6_tx_sample.csv", index=False)
    actor_candidates.to_csv(output_dir / "V6_actor_candidates.csv", index=False)
    return {"actor_events": actor_events, "tx_sample": tx_sample, "coverage": coverage, "actor_candidates": actor_candidates}
