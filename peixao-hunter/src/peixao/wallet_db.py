from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3

import pandas as pd


SCORE_VERSION = "V1"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _value(row, key: str):
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


def _float_value(row, key: str) -> float | None:
    value = _value(row, key)
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _payload(row) -> str:
    raw = {}
    for key, value in dict(row).items():
        value = _value(row, key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            raw[str(key)] = value
        else:
            raw[str(key)] = str(value)
    return json.dumps(raw, ensure_ascii=False, sort_keys=True)


def initialize_master_wallet_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wallets (
                address TEXT PRIMARY KEY,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                times_spotted INTEGER NOT NULL DEFAULT 0,
                last_score REAL,
                last_tier TEXT,
                last_evidence_state TEXT,
                last_gate_status TEXT,
                last_sample_confidence REAL,
                last_win_rate REAL,
                last_profit_30d REAL,
                last_updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wallet_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                score_version TEXT NOT NULL,
                peixao_score REAL,
                tier TEXT,
                evidence_state TEXT,
                evidence_coverage REAL,
                sample_confidence REAL,
                win_rate REAL,
                sample_size REAL,
                realized_profit_30d REAL,
                realized_roi_30d REAL,
                distinct_tokens REAL,
                independent_cross_token_hits REAL,
                risk_flags TEXT,
                payload_json TEXT NOT NULL,
                UNIQUE(address, observed_at, score_version),
                FOREIGN KEY(address) REFERENCES wallets(address)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wallet_snapshots_address_time ON wallet_snapshots(address, observed_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wallet_snapshots_score ON wallet_snapshots(peixao_score DESC)")


def record_wallet_snapshots(
    ranked_v1: pd.DataFrame,
    db_path: Path,
    *,
    observed_at: str | None = None,
) -> dict:
    """Append one historical snapshot per scored wallet and update the master row.

    The database lives under PEIXAO_DATA_DIR, so Railway's persistent volume
    keeps wallet history across deployments. Replaying the same timestamp is
    idempotent because snapshots are unique by wallet/time/score-version.
    """
    observed_at = observed_at or _now()
    initialize_master_wallet_db(db_path)

    if ranked_v1.empty or "address" not in ranked_v1.columns:
        return {
            "status": "DONE",
            "wallets_seen": 0,
            "snapshots_inserted": 0,
            "database": str(db_path),
        }

    inserted = 0
    seen = 0
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA busy_timeout = 5000")
        for row in ranked_v1.to_dict("records"):
            address = str(row.get("address", "") or "").strip().lower()
            score = _float_value(row, "peixao_score_v1")
            if not address or score is None:
                continue

            seen += 1
            tier = str(_value(row, "peixao_tier_v1") or "")
            evidence_state = str(_value(row, "evidence_state") or "")
            gate_status = str(_value(row, "v1_gate_status") or "")
            sample_confidence = _float_value(row, "sample_confidence")
            win_rate = _float_value(row, "gmgn_winrate_30d")
            profit30 = _float_value(row, "realized_profit_30d")

            conn.execute(
                """
                INSERT OR IGNORE INTO wallets (
                    address, first_seen, last_seen, times_spotted,
                    last_score, last_tier, last_evidence_state, last_gate_status,
                    last_sample_confidence, last_win_rate, last_profit_30d, last_updated_at
                ) VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    address,
                    observed_at,
                    observed_at,
                    score,
                    tier,
                    evidence_state,
                    gate_status,
                    sample_confidence,
                    win_rate,
                    profit30,
                    observed_at,
                ),
            )

            cur = conn.execute(
                """
                INSERT OR IGNORE INTO wallet_snapshots (
                    address, observed_at, score_version, peixao_score, tier,
                    evidence_state, evidence_coverage, sample_confidence,
                    win_rate, sample_size, realized_profit_30d, realized_roi_30d,
                    distinct_tokens, independent_cross_token_hits, risk_flags, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    address,
                    observed_at,
                    SCORE_VERSION,
                    score,
                    tier,
                    evidence_state,
                    _float_value(row, "evidence_coverage"),
                    sample_confidence,
                    win_rate,
                    _float_value(row, "gmgn_token_num_30d"),
                    profit30,
                    _float_value(row, "realized_roi_30d"),
                    _float_value(row, "distinct_tokens"),
                    _float_value(row, "independent_cross_token_hits"),
                    str(_value(row, "v1_risk_flags") or ""),
                    _payload(row),
                ),
            )

            if cur.rowcount:
                inserted += 1
                conn.execute(
                    """
                    UPDATE wallets
                    SET last_seen = ?,
                        times_spotted = times_spotted + 1,
                        last_score = ?,
                        last_tier = ?,
                        last_evidence_state = ?,
                        last_gate_status = ?,
                        last_sample_confidence = ?,
                        last_win_rate = ?,
                        last_profit_30d = ?,
                        last_updated_at = ?
                    WHERE address = ?
                    """,
                    (
                        observed_at,
                        score,
                        tier,
                        evidence_state,
                        gate_status,
                        sample_confidence,
                        win_rate,
                        profit30,
                        observed_at,
                        address,
                    ),
                )

    return {
        "status": "DONE",
        "wallets_seen": int(seen),
        "snapshots_inserted": int(inserted),
        "database": str(db_path),
    }


def record_ranked_v1_csv(ranked_v1_path: Path, db_path: Path, *, observed_at: str | None = None) -> dict:
    if not ranked_v1_path.is_file():
        initialize_master_wallet_db(db_path)
        return {
            "status": "SKIPPED_NO_V1_RANKING",
            "wallets_seen": 0,
            "snapshots_inserted": 0,
            "database": str(db_path),
        }
    try:
        ranked = pd.read_csv(ranked_v1_path)
    except pd.errors.EmptyDataError:
        ranked = pd.DataFrame()
    return record_wallet_snapshots(ranked, db_path, observed_at=observed_at)
