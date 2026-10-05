from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3

import pandas as pd

from .selective_alpha import SELECTIVE_SCORE_VERSION


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _value(row: dict, key: str):
    value = row.get(key)
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    return value


def _float(row: dict, key: str):
    value = _value(row, key)
    try:
        return None if value is None else float(value)
    except Exception:
        return None


def _payload(row: dict) -> str:
    out = {}
    for key in row:
        value = _value(row, key)
        out[str(key)] = value if isinstance(value, (str, int, float, bool)) or value is None else str(value)
    return json.dumps(out, ensure_ascii=False, sort_keys=True)


def record_selective_stage1_csv(stage1_path: Path, db_path: Path, *, observed_at: str | None = None) -> dict:
    observed_at = observed_at or _now()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not stage1_path.is_file():
        return {"status": "SKIPPED_NO_SELECTIVE_STAGE1", "wallets_seen": 0, "snapshots_inserted": 0}
    try:
        frame = pd.read_csv(stage1_path)
    except pd.errors.EmptyDataError:
        frame = pd.DataFrame()
    if frame.empty or "address" not in frame.columns:
        return {"status": "DONE", "wallets_seen": 0, "snapshots_inserted": 0, "database": str(db_path)}

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS selective_wallet_state (
                address TEXT PRIMARY KEY,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                times_scored INTEGER NOT NULL DEFAULT 0,
                last_score REAL,
                last_tier TEXT,
                last_selectivity REAL,
                last_profile TEXT,
                last_entries_per_week REAL,
                last_win_rate REAL,
                last_pnl_per_position REAL,
                last_repeatability REAL,
                last_evidence_coverage REAL,
                last_deep_dive_candidate INTEGER NOT NULL DEFAULT 0,
                last_updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS selective_wallet_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                score_version TEXT NOT NULL,
                score REAL,
                tier TEXT,
                selectivity_score REAL,
                profile TEXT,
                entries_per_week REAL,
                frequency_score REAL,
                accuracy_score REAL,
                pnl_per_position REAL,
                pnl_per_position_score REAL,
                repeatability_score REAL,
                evidence_coverage REAL,
                win_rate REAL,
                sample_confidence REAL,
                deep_dive_candidate INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL,
                UNIQUE(address, observed_at, score_version)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_selective_score ON selective_wallet_snapshots(score DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_selective_wallet_time ON selective_wallet_snapshots(address, observed_at)")

        seen = 0
        inserted = 0
        for row in frame.to_dict("records"):
            address = str(row.get("address", "") or "").strip()
            score = _float(row, "selective_alpha_score")
            if not address or score is None:
                continue
            seen += 1
            pnl_usd = _float(row, "selective_pnl_per_position_usd")
            conn.execute(
                """
                INSERT OR IGNORE INTO selective_wallet_state (
                    address, first_seen, last_seen, times_scored, last_score, last_tier,
                    last_selectivity, last_profile, last_entries_per_week, last_win_rate,
                    last_pnl_per_position, last_repeatability, last_evidence_coverage,
                    last_deep_dive_candidate, last_updated_at
                ) VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    address, observed_at, observed_at, score,
                    str(_value(row, "selective_alpha_tier") or ""),
                    _float(row, "selective_score"), str(_value(row, "selective_profile") or ""),
                    _float(row, "selective_new_positions_per_week"), _float(row, "selective_win_rate"),
                    pnl_usd, _float(row, "selective_repeatability"),
                    _float(row, "selective_evidence_coverage"),
                    1 if bool(_value(row, "selective_deep_dive_candidate")) else 0,
                    observed_at,
                ),
            )
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO selective_wallet_snapshots (
                    address, observed_at, score_version, score, tier, selectivity_score,
                    profile, entries_per_week, frequency_score, accuracy_score,
                    pnl_per_position, pnl_per_position_score, repeatability_score,
                    evidence_coverage, win_rate, sample_confidence, deep_dive_candidate,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    address, observed_at, SELECTIVE_SCORE_VERSION, score,
                    str(_value(row, "selective_alpha_tier") or ""), _float(row, "selective_score"),
                    str(_value(row, "selective_profile") or ""), _float(row, "selective_new_positions_per_week"),
                    _float(row, "selective_frequency"), _float(row, "selective_accuracy"),
                    pnl_usd, _float(row, "selective_pnl_per_position_score"),
                    _float(row, "selective_repeatability"), _float(row, "selective_evidence_coverage"),
                    _float(row, "selective_win_rate"), _float(row, "selective_sample_confidence"),
                    1 if bool(_value(row, "selective_deep_dive_candidate")) else 0, _payload(row),
                ),
            )
            if cur.rowcount:
                inserted += 1
                conn.execute(
                    """
                    UPDATE selective_wallet_state SET
                        last_seen = ?, times_scored = times_scored + 1, last_score = ?,
                        last_tier = ?, last_selectivity = ?, last_profile = ?,
                        last_entries_per_week = ?, last_win_rate = ?, last_pnl_per_position = ?,
                        last_repeatability = ?, last_evidence_coverage = ?,
                        last_deep_dive_candidate = ?, last_updated_at = ?
                    WHERE address = ?
                    """,
                    (
                        observed_at, score, str(_value(row, "selective_alpha_tier") or ""),
                        _float(row, "selective_score"), str(_value(row, "selective_profile") or ""),
                        _float(row, "selective_new_positions_per_week"), _float(row, "selective_win_rate"),
                        pnl_usd, _float(row, "selective_repeatability"),
                        _float(row, "selective_evidence_coverage"),
                        1 if bool(_value(row, "selective_deep_dive_candidate")) else 0,
                        observed_at, address,
                    ),
                )
    return {
        "status": "DONE",
        "wallets_seen": seen,
        "snapshots_inserted": inserted,
        "score_version": SELECTIVE_SCORE_VERSION,
        "database": str(db_path),
    }
