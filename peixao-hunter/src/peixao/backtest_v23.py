from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import time

import pandas as pd

from .evidence_ledger import normalize_chain, wallet_key


HORIZONS = (7, 14, 30)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _num(value):
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=20)
    conn.execute("PRAGMA busy_timeout=20000")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS score_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_at TEXT NOT NULL,
            snapshot_epoch INTEGER NOT NULL,
            wallet_key TEXT NOT NULL,
            chain TEXT NOT NULL,
            address TEXT NOT NULL,
            score REAL,
            tier TEXT,
            gate TEXT,
            realized_profit_30d REAL,
            realized_roi_30d REAL,
            win_rate REAL,
            sample_size REAL,
            discovery_score REAL,
            UNIQUE(snapshot_epoch, wallet_key)
        );
        CREATE INDEX IF NOT EXISTS idx_score_snapshots_due ON score_snapshots(snapshot_epoch, wallet_key);
        CREATE TABLE IF NOT EXISTS backtest_outcomes (
            snapshot_id INTEGER NOT NULL,
            wallet_key TEXT NOT NULL,
            horizon_days INTEGER NOT NULL,
            evaluated_at TEXT NOT NULL,
            score_start REAL,
            score_end REAL,
            pnl_start REAL,
            pnl_end REAL,
            pnl_delta REAL,
            roi_start REAL,
            roi_end REAL,
            gate_end TEXT,
            outcome_method TEXT NOT NULL,
            PRIMARY KEY (snapshot_id, horizon_days)
        );
        """
    )
    conn.commit()
    return conn


def _stage_rows(stage_path: Path) -> tuple[pd.DataFrame, dict[str, dict]]:
    try:
        frame = pd.read_csv(stage_path) if stage_path.is_file() else pd.DataFrame()
    except Exception:
        frame = pd.DataFrame()
    mapping: dict[str, dict] = {}
    if frame.empty or "address" not in frame.columns:
        return frame, mapping
    for row in frame.to_dict("records"):
        chain = normalize_chain(row.get("chain"))
        if chain == "unknown" and str(row.get("selective_input_source", "")).lower() == "legacy_v6":
            chain = "robinhood"
        address = str(row.get("address", "") or "").strip()
        if address:
            mapping[wallet_key(chain, address)] = row
    return frame, mapping


def _band(score) -> str:
    value = _num(score)
    if value is None:
        return "UNENRICHED"
    if value >= 90:
        return "S"
    if value >= 80:
        return "A+"
    if value >= 70:
        return "A"
    if value >= 60:
        return "B"
    if value >= 50:
        return "C"
    return "D"


def update_backtest(stage_path: Path, db_path: Path, output_path: Path) -> dict:
    frame, current = _stage_rows(stage_path)
    if frame.empty:
        pd.DataFrame().to_csv(output_path, index=False)
        return {"status": "DONE_EMPTY", "snapshots_added": 0, "outcomes_added": 0, "summary_rows": 0}

    now = int(time.time())
    conn = _connect(db_path)
    outcomes_added = 0
    added = 0
    try:
        snapshots = conn.execute(
            "SELECT snapshot_id,snapshot_epoch,wallet_key,score,realized_profit_30d,realized_roi_30d FROM score_snapshots"
        ).fetchall()
        existing = {
            (int(r[0]), int(r[1]))
            for r in conn.execute("SELECT snapshot_id,horizon_days FROM backtest_outcomes").fetchall()
        }
        for snapshot_id, snapshot_epoch, key, score_start, pnl_start, roi_start in snapshots:
            row = current.get(str(key))
            if row is None:
                continue
            for horizon in HORIZONS:
                if (int(snapshot_id), horizon) in existing or now - int(snapshot_epoch) < horizon * 86400:
                    continue
                score_end = _num(row.get("selective_alpha_score"))
                pnl_end = _num(row.get("realized_profit_30d"))
                roi_end = _num(row.get("realized_roi_30d"))
                pnl_delta = None if pnl_start is None or pnl_end is None else pnl_end - float(pnl_start)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO backtest_outcomes
                    (snapshot_id,wallet_key,horizon_days,evaluated_at,score_start,score_end,
                     pnl_start,pnl_end,pnl_delta,roi_start,roi_end,gate_end,outcome_method)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        snapshot_id, key, horizon, _now_iso(), score_start, score_end,
                        pnl_start, pnl_end, pnl_delta, roi_start, roi_end,
                        str(row.get("alpha22_gate_status", "") or ""),
                        "rolling_30d_realized_pnl_delta_proxy",
                    ),
                )
                outcomes_added += 1

        snapshot_epoch = now - (now % 3600)
        for row in frame.to_dict("records"):
            score = _num(row.get("selective_alpha_score"))
            if score is None:
                continue
            chain = normalize_chain(row.get("chain"))
            if chain == "unknown" and str(row.get("selective_input_source", "")).lower() == "legacy_v6":
                chain = "robinhood"
            address = str(row.get("address", "") or "").strip()
            if not address:
                continue
            before = conn.total_changes
            conn.execute(
                """
                INSERT OR IGNORE INTO score_snapshots
                (snapshot_at,snapshot_epoch,wallet_key,chain,address,score,tier,gate,
                 realized_profit_30d,realized_roi_30d,win_rate,sample_size,discovery_score)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    _now_iso(), snapshot_epoch, wallet_key(chain, address), chain, address, score,
                    str(row.get("selective_alpha_tier", _band(score)) or _band(score)),
                    str(row.get("alpha22_gate_status", "") or ""),
                    _num(row.get("realized_profit_30d")), _num(row.get("realized_roi_30d")),
                    _num(row.get("win_rate")), _num(row.get("alpha22_sample_size")),
                    _num(row.get("discovery_score")),
                ),
            )
            added += int(conn.total_changes > before)
        conn.commit()
        rows = conn.execute("SELECT horizon_days,score_start,pnl_delta,gate_end FROM backtest_outcomes").fetchall()
    finally:
        conn.close()

    summary_rows: list[dict] = []
    if rows:
        raw = pd.DataFrame(rows, columns=["horizon_days", "score_start", "pnl_delta", "gate_end"])
        raw["start_tier"] = raw["score_start"].map(_band)
        for (horizon, tier), group in raw.groupby(["horizon_days", "start_tier"], dropna=False):
            pnl = pd.to_numeric(group["pnl_delta"], errors="coerce")
            summary_rows.append({
                "horizon_days": int(horizon),
                "start_tier": str(tier),
                "observations": int(len(group)),
                "pnl_proxy_available": int(pnl.notna().sum()),
                "positive_pnl_proxy_rate": None if not pnl.notna().any() else round(float((pnl.dropna() > 0).mean()), 4),
                "median_pnl_delta_proxy": None if not pnl.notna().any() else round(float(pnl.dropna().median()), 2),
                "pass_rate_end": round(float(group["gate_end"].astype(str).eq("PASS").mean()), 4),
            })
    summary = pd.DataFrame(summary_rows)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(output_path, index=False)
    return {
        "status": "DONE", "snapshots_added": int(added), "outcomes_added": int(outcomes_added),
        "summary_rows": int(len(summary)), "output": str(output_path),
    }
