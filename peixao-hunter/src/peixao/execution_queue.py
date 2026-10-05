from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import math
import os
import threading
import time

import pandas as pd

from .state import atomic_csv


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


def _read_csv(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path) if path.is_file() else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def _num(row: dict, names: tuple[str, ...], default: float = 0.0) -> float:
    for name in names:
        try:
            value = row.get(name)
            if value is not None and not pd.isna(value):
                return float(value)
        except Exception:
            continue
    return float(default)


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _priority(row: dict, *, seen_count: int, cross_chain_hits: int) -> float:
    hits = _num(row, ("independent_cross_token_hits", "cross_token_hits", "distinct_tokens"), 0.0)
    events = _num(
        row,
        (
            "total_transfer_events",
            "quicknode_recent_signatures",
            "quicknode_holder_entries",
            "total_trades",
        ),
        0.0,
    )
    discovery = _num(row, ("discovery_score",), 0.0)
    activity = _num(row, ("quicknode_activity_score",), 0.0)
    weighted = 100.0 * _num(row, ("weighted_cross_token_score",), 0.0)
    risk = 20.0 if _truthy(row.get("risk_tagged")) else 0.0
    repeat_bonus = min(15.0, max(0, seen_count - 1) * 2.5)
    chain_bonus = max(0, cross_chain_hits - 1) * 10.0
    score = (
        18.0 * min(5.0, hits)
        + 3.0 * math.log1p(max(0.0, events))
        + 0.22 * discovery
        + 0.14 * activity
        + 0.12 * weighted
        + repeat_bonus
        + chain_bonus
        - risk
    )
    return round(max(0.0, min(100.0, score)), 2)


def _merge_source_rows(left: dict | None, right: dict) -> dict:
    """Merge duplicate discovery evidence without double-counting a queue cycle."""
    merged = dict(left or {})
    merged.update(right)
    for key in (
        "independent_cross_token_hits",
        "cross_token_hits",
        "distinct_tokens",
        "total_transfer_events",
        "quicknode_recent_signatures",
        "quicknode_holder_entries",
        "total_trades",
        "discovery_score",
        "quicknode_activity_score",
        "weighted_cross_token_score",
    ):
        values = []
        for source in (left or {}, right):
            try:
                value = source.get(key)
                if value is not None and not pd.isna(value):
                    values.append(float(value))
            except Exception:
                pass
        if values:
            merged[key] = max(values)
    discovery_sources = []
    for source in (left or {}, right):
        value = str(source.get("discovery_source", "") or "").strip()
        if value and value not in discovery_sources:
            discovery_sources.append(value)
    if discovery_sources:
        merged["discovery_source"] = "+".join(discovery_sources)
    if str((left or {}).get("classification", "")) == "EOA_NO_CODE" or str(right.get("classification", "")) == "EOA_NO_CODE":
        merged["classification"] = "EOA_NO_CODE"
    return merged


def _build_execution_queue(
    output_dir: Path,
    state_dir: Path,
    *,
    stale_seconds: int = 7 * 86400,
) -> dict:
    """Persist and rank discovery candidates so expensive validators work on fresh/high-signal wallets first."""
    output_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "execution_priority_queue.json"
    out_path = output_dir / "V22_execution_queue.csv"
    state = _load_json(state_path)
    entries = state.get("entries") if isinstance(state.get("entries"), dict) else {}
    now = int(time.time())
    now_iso = _now_iso()

    source_specs = (
        ("solana", output_dir / "V22_quicknode_wallet_candidates.csv"),
        ("base", output_dir / "V22_base_wallet_candidates.csv"),
        ("robinhood", output_dir / "V22_robinhood_wallet_candidates.csv"),
        ("robinhood", output_dir / "V22_robinhood_stream_wallet_candidates.csv"),
    )
    cycle_rows: dict[str, dict] = {}
    cycle_meta: dict[str, tuple[str, str]] = {}

    for chain, path in source_specs:
        frame = _read_csv(path)
        if frame.empty or "address" not in frame.columns:
            continue
        for row in frame.to_dict("records"):
            address = str(row.get("address", "") or "").strip()
            if not address or address.lower() == "nan":
                continue
            normalized = address if chain == "solana" else address.lower()
            key = f"{chain}:{normalized}"
            cycle_rows[key] = _merge_source_rows(cycle_rows.get(key), row)
            cycle_meta[key] = (chain, normalized)

    current_keys = set(cycle_rows)
    for key, row in cycle_rows.items():
        chain, normalized = cycle_meta[key]
        previous = entries.get(key) if isinstance(entries.get(key), dict) else {}
        seen_count = int(previous.get("seen_count", 0) or 0) + 1
        entries[key] = {
            "chain": chain,
            "address": normalized,
            "first_seen_epoch": int(previous.get("first_seen_epoch", now) or now),
            "first_seen_at": str(previous.get("first_seen_at", now_iso) or now_iso),
            "last_seen_epoch": now,
            "last_seen_at": now_iso,
            "seen_count": seen_count,
            "row": _merge_source_rows(previous.get("row") if isinstance(previous.get("row"), dict) else {}, row),
        }

    cutoff = now - max(3600, int(stale_seconds))
    entries = {
        key: value
        for key, value in entries.items()
        if isinstance(value, dict) and int(value.get("last_seen_epoch", 0) or 0) >= cutoff
    }

    address_chains: dict[str, set[str]] = {}
    for value in entries.values():
        chain = str(value.get("chain", "") or "")
        address = str(value.get("address", "") or "")
        if not chain or not address:
            continue
        if address.lower().startswith("0x") and len(address) == 42:
            address_chains.setdefault(address.lower(), set()).add(chain)

    rows: list[dict] = []
    for key, value in entries.items():
        chain = str(value.get("chain", "") or "")
        address = str(value.get("address", "") or "")
        source_row = value.get("row") if isinstance(value.get("row"), dict) else {}
        cross_chain_hits = len(address_chains.get(address.lower(), {chain})) if address.lower().startswith("0x") else 1
        seen_count = int(value.get("seen_count", 1) or 1)
        priority = _priority(source_row, seen_count=seen_count, cross_chain_hits=cross_chain_hits)
        rows.append({
            **source_row,
            "address": address,
            "chain": chain,
            "execution_priority_score": priority,
            "execution_seen_count": seen_count,
            "execution_cross_chain_hits": int(cross_chain_hits),
            "execution_first_seen_at": value.get("first_seen_at"),
            "execution_last_seen_at": value.get("last_seen_at"),
            "execution_seen_this_cycle": key in current_keys,
        })

    queue = pd.DataFrame(rows)
    if not queue.empty:
        queue = queue.sort_values(
            ["execution_priority_score", "execution_cross_chain_hits", "execution_seen_count"],
            ascending=[False, False, False],
            kind="mergesort",
        ).reset_index(drop=True)
    atomic_csv(queue, out_path)

    by_chain: dict[str, int] = {}
    for chain in ("solana", "base", "robinhood"):
        chain_path = output_dir / f"V22_{chain}_wallet_priority.csv"
        subset = queue[queue["chain"].eq(chain)].copy() if not queue.empty and "chain" in queue.columns else pd.DataFrame()
        atomic_csv(subset, chain_path)
        by_chain[chain] = int(len(subset))

    _atomic_json(state_path, {"updated_epoch": now, "updated_at": now_iso, "entries": entries})
    return {
        "status": "DONE",
        "wallets": int(len(queue)),
        "current_cycle_wallets": int(len(current_keys)),
        "by_chain": by_chain,
        "output": str(out_path),
        "stale_seconds": int(stale_seconds),
    }


# A fila é reconstruída pelo ciclo principal e pela thread do stream.
EXECUTION_QUEUE_LOCK = threading.RLock()


def build_execution_queue(*args, **kwargs) -> dict:
    with EXECUTION_QUEUE_LOCK:
        return _build_execution_queue(*args, **kwargs)
