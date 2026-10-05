from __future__ import annotations

from pathlib import Path
import math
import time

import pandas as pd

from .evidence_ledger import monitor_state_map, normalize_chain, provider_health, wallet_key
from .execution_queue import EXECUTION_QUEUE_LOCK, build_execution_queue
from .monitor_state_v23 import activity_marker
from .state import atomic_csv


def _read(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path) if path.is_file() else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def _has(row: dict | None, key: str) -> bool:
    if not row:
        return False
    value = row.get(key)
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except Exception:
        pass
    return str(value).strip().lower() not in {"", "nan", "none", "null"}


def evidence_gap_score(row: dict | None) -> float:
    if not row:
        return 100.0
    score = 0.0
    if not (_has(row, "win_rate") or _has(row, "gmgn_winrate_30d")):
        score += 50.0
    if not _has(row, "closed_positions"):
        score += 20.0
    if not _has(row, "realized_profit_30d"):
        score += 15.0
    if not _has(row, "repeatability_score"):
        score += 10.0
    if not _has(row, "new_positions_per_week"):
        score += 5.0
    return score


def _stage_map(output_dir: Path) -> dict[str, dict]:
    completed = output_dir / "V22S_wallet_stage1_completed.csv"
    live = output_dir / "V22S_wallet_stage1.csv"
    frame = _read(completed if completed.is_file() else live)
    result: dict[str, dict] = {}
    if frame.empty or "address" not in frame.columns:
        return result
    for row in frame.to_dict("records"):
        chain = normalize_chain(row.get("chain"))
        if chain == "unknown" and str(row.get("selective_input_source", "")).lower() == "legacy_v6":
            chain = "robinhood"
        address = str(row.get("address", "") or "").strip()
        if address:
            result[wallet_key(chain, address)] = row
    return result


def build_adaptive_execution_queue(
    output_dir: Path,
    state_dir: Path,
    db_path: Path,
    *,
    stale_seconds: int = 7 * 86400,
) -> dict:
    with EXECUTION_QUEUE_LOCK:
        return _build_adaptive_execution_queue(output_dir, state_dir, db_path, stale_seconds=stale_seconds)


def _build_adaptive_execution_queue(
    output_dir: Path,
    state_dir: Path,
    db_path: Path,
    *,
    stale_seconds: int = 7 * 86400,
) -> dict:
    base = build_execution_queue(output_dir, state_dir, stale_seconds=stale_seconds)
    queue = _read(output_dir / "V22_execution_queue.csv")
    out_path = output_dir / "V22_execution_queue_adaptive.csv"
    if queue.empty or "address" not in queue.columns:
        atomic_csv(queue, out_path)
        return {
            **base, "adaptive": True, "due_wallets": 0,
            "monitor_suppressed": 0, "output": str(out_path),
        }

    stage = _stage_map(output_dir)
    monitors = monitor_state_map(db_path)
    now = int(time.time())
    rows: list[dict] = []
    due = suppressed = 0

    for row in queue.to_dict("records"):
        chain = normalize_chain(row.get("chain"))
        address = str(row.get("address", "") or "").strip()
        key = wallet_key(chain, address)
        prior = stage.get(key)
        monitor = monitors.get(key, {})
        current_activity = activity_marker(row)
        previous_activity = int(monitor.get("last_activity_marker", 0) or 0)
        new_activity = current_activity > previous_activity
        next_refresh = int(monitor.get("next_refresh_epoch", 0) or 0)
        already_scored = _has(prior, "selective_alpha_score")
        monitor_due = (not already_scored) or new_activity or not next_refresh or now >= next_refresh
        due += int(monitor_due)
        suppressed += int(not monitor_due)
        gap = evidence_gap_score(prior)
        base_score = float(row.get("execution_priority_score", 0) or 0)
        adaptive = base_score + 0.65 * gap + (20.0 if new_activity else 0.0) - (0.0 if monitor_due else 100.0)
        row.update({
            "wallet_key": key,
            "evidence_gap_score": round(gap, 2),
            "monitor_due": bool(monitor_due),
            "monitor_new_activity": bool(new_activity),
            "monitor_activity_marker": int(current_activity),
            "monitor_next_refresh_epoch": next_refresh,
            "adaptive_priority_score": round(adaptive, 2),
        })
        rows.append(row)

    out = pd.DataFrame(rows).sort_values(
        ["monitor_due", "adaptive_priority_score", "execution_priority_score"],
        ascending=[False, False, False], kind="mergesort",
    ).reset_index(drop=True)
    atomic_csv(out, out_path)
    atomic_csv(out, output_dir / "V22_execution_queue.csv")

    by_chain: dict[str, int] = {}
    due_by_chain: dict[str, int] = {}
    for chain in ("solana", "base", "robinhood"):
        subset = out[out["chain"].astype(str).str.lower().eq(chain)].copy()
        due_subset = subset[subset["monitor_due"].eq(True)].copy()
        atomic_csv(due_subset, output_dir / f"V22_{chain}_wallet_priority.csv")
        by_chain[chain] = int(len(subset))
        due_by_chain[chain] = int(len(due_subset))
    return {
        **base, "status": "DONE", "adaptive": True, "wallets": int(len(out)),
        "due_wallets": due, "monitor_suppressed": suppressed,
        "by_chain": by_chain, "due_by_chain": due_by_chain, "output": str(out_path),
    }


def adaptive_provider_limit(
    db_path: Path,
    provider: str,
    chain: str,
    base_limit: int,
    *,
    backlog: int = 0,
) -> int:
    base = max(0, int(base_limit))
    if base == 0:
        return 0
    health = provider_health(db_path, provider, chain, hours=24)
    attempted = int(health.get("attempted", 0) or 0)
    errors = int(health.get("errors", 0) or 0)
    throttled = int(health.get("throttled_statuses", 0) or 0)
    success = health.get("success_rate")
    if throttled:
        return max(1, int(math.ceil(base * 0.50)))
    if attempted >= 5 and success is not None and float(success) < 0.45:
        return max(1, int(math.ceil(base * 0.60)))
    if attempted >= 5 and errors / max(1, attempted) > 0.25:
        return max(1, int(math.ceil(base * 0.70)))
    if attempted >= 10 and success is not None and float(success) >= 0.85 and backlog > base * 2:
        return min(base + 10, int(math.ceil(base * 1.25)))
    return base
