from __future__ import annotations

from pathlib import Path
import time

import pandas as pd

from .evidence_ledger import _connect, _float, _now_iso, normalize_address, normalize_chain, wallet_key


def activity_marker(row: dict) -> int:
    for name in ("total_transfer_events", "quicknode_recent_signatures", "sampled_txs", "total_trades"):
        value = _float(row.get(name))
        if value is not None and value >= 0:
            return int(value)
    return 0


def update_monitor_from_stage(
    db_path: Path,
    stage_path: Path,
    execution_queue_path: Path | None = None,
    *,
    refresh_seconds: int = 86400,
) -> dict:
    try:
        stage = pd.read_csv(stage_path) if stage_path.is_file() else pd.DataFrame()
    except Exception:
        stage = pd.DataFrame()
    if stage.empty or "address" not in stage.columns:
        return {"status": "DONE_EMPTY", "tracked": 0}

    queue_meta: dict[str, dict] = {}
    if execution_queue_path and execution_queue_path.is_file():
        try:
            queue = pd.read_csv(execution_queue_path)
        except Exception:
            queue = pd.DataFrame()
        if not queue.empty and {"address", "chain"}.issubset(queue.columns):
            for row in queue.to_dict("records"):
                key = wallet_key(str(row.get("chain")), str(row.get("address")))
                queue_meta[key] = {
                    "activity": activity_marker(row),
                    "new_activity": str(row.get("monitor_new_activity", "")).strip().lower() in {"true", "1", "yes"},
                }

    now = int(time.time())
    ttl = max(3600, int(refresh_seconds))
    conn = _connect(db_path)
    tracked = 0
    try:
        for row in stage.to_dict("records"):
            chain = normalize_chain(row.get("chain"))
            if chain == "unknown" and str(row.get("selective_input_source", "")).lower() == "legacy_v6":
                chain = "robinhood"
            address = normalize_address(chain, row.get("address"))
            score = _float(row.get("selective_alpha_score"))
            if not address or score is None:
                continue
            key = wallet_key(chain, address)
            previous = conn.execute(
                "SELECT last_activity_marker,next_refresh_epoch FROM wallet_monitor WHERE wallet_key=?",
                (key,),
            ).fetchone()
            meta = queue_meta.get(key, {})
            marker = int(meta.get("activity", 0) or 0)
            if previous:
                marker = max(int(previous[0] or 0), marker)
            next_refresh = now + ttl
            conn.execute(
                """
                INSERT INTO wallet_monitor
                (wallet_key,chain,address,last_validated_at,last_validated_epoch,
                 last_activity_marker,next_refresh_epoch,last_score,last_gate)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(wallet_key) DO UPDATE SET
                    chain=excluded.chain,address=excluded.address,
                    last_validated_at=excluded.last_validated_at,
                    last_validated_epoch=excluded.last_validated_epoch,
                    last_activity_marker=excluded.last_activity_marker,
                    next_refresh_epoch=excluded.next_refresh_epoch,
                    last_score=excluded.last_score,last_gate=excluded.last_gate
                """,
                (
                    key, chain, address, _now_iso(), now, marker, next_refresh, score,
                    str(row.get("alpha22_gate_status", "") or ""),
                ),
            )
            tracked += 1
        conn.commit()
    finally:
        conn.close()
    return {"status": "DONE", "tracked": tracked, "refresh_seconds": ttl}
