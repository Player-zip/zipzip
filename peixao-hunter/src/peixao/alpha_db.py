from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3

import pandas as pd


SCORE_VERSION = "V2.2"


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


def _float(row, key: str) -> float | None:
    value = _value(row, key)
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _payload(row) -> str:
    raw = {}
    for key in dict(row):
        value = _value(row, key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            raw[str(key)] = value
        else:
            raw[str(key)] = str(value)
    return json.dumps(raw, ensure_ascii=False, sort_keys=True)


def initialize_alpha_db(db_path: Path) -> None:
    """Create V2.2 tables alongside the frozen V1 master tables."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alpha22_wallet_state (
                address TEXT PRIMARY KEY,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                times_scored INTEGER NOT NULL DEFAULT 0,
                last_score REAL,
                last_tier TEXT,
                last_evidence_state TEXT,
                last_gate_status TEXT,
                last_sample_size REAL,
                last_sample_source TEXT,
                last_sample_confidence REAL,
                last_win_rate REAL,
                last_wilson_lb REAL,
                last_cross_token_source TEXT,
                last_evidence_coverage REAL,
                last_updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alpha22_wallet_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                score_version TEXT NOT NULL,
                alpha_score REAL,
                tier TEXT,
                evidence_state TEXT,
                evidence_coverage REAL,
                gate_status TEXT,
                sample_size REAL,
                sample_size_source TEXT,
                sample_confidence REAL,
                win_rate REAL,
                winrate_wilson_lb REAL,
                repeatability REAL,
                statistical_edge REAL,
                weighted_cross_token_edge REAL,
                cross_token_source TEXT,
                profit_quality REAL,
                temporal_consistency REAL,
                pnl_roi REAL,
                closed_positions REAL,
                total_trades REAL,
                profit_hhi REAL,
                largest_win_share REAL,
                top3_profit_share REAL,
                median_pnl_per_token REAL,
                independent_alpha_clusters REAL,
                independent_cross_token_hits REAL,
                deep_dive_candidate INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL,
                UNIQUE(address, observed_at, score_version)
            )
            """
        )
        # T0 foundation for the Token Radar / survivorship-bias-safe backtest.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS token_signal_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chain TEXT NOT NULL,
                token_address TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                source TEXT NOT NULL,
                symbol TEXT,
                price_usd REAL,
                market_cap_usd REAL,
                liquidity_usd REAL,
                volume_24h_usd REAL,
                token_age_seconds REAL,
                smart_buys REAL,
                smart_sells REAL,
                holders REAL,
                snipers REAL,
                bundlers REAL,
                risk_score REAL,
                payload_json TEXT NOT NULL,
                UNIQUE(chain, token_address, observed_at, source)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alpha22_wallet_time ON alpha22_wallet_snapshots(address, observed_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alpha22_score ON alpha22_wallet_snapshots(alpha_score DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_token_signal_time ON token_signal_snapshots(chain, token_address, observed_at)")


def record_alpha22_stage1_csv(
    stage1_path: Path,
    db_path: Path,
    *,
    observed_at: str | None = None,
) -> dict:
    observed_at = observed_at or _now()
    initialize_alpha_db(db_path)
    if not stage1_path.is_file():
        return {"status": "SKIPPED_NO_ALPHA22_STAGE1", "wallets_seen": 0, "snapshots_inserted": 0, "database": str(db_path)}

    try:
        frame = pd.read_csv(stage1_path)
    except pd.errors.EmptyDataError:
        frame = pd.DataFrame()
    if frame.empty or "address" not in frame.columns:
        return {"status": "DONE", "wallets_seen": 0, "snapshots_inserted": 0, "database": str(db_path)}

    seen = 0
    inserted = 0
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA busy_timeout = 5000")
        for row in frame.to_dict("records"):
            address = str(row.get("address", "") or "").strip().lower()
            score = _float(row, "alpha22_score")
            if not address or score is None:
                continue
            seen += 1
            win_rate = _float(row, "gmgn_winrate_30d")
            conn.execute(
                """
                INSERT OR IGNORE INTO alpha22_wallet_state (
                    address, first_seen, last_seen, times_scored, last_score, last_tier,
                    last_evidence_state, last_gate_status, last_sample_size, last_sample_source,
                    last_sample_confidence, last_win_rate, last_wilson_lb, last_cross_token_source,
                    last_evidence_coverage, last_updated_at
                ) VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    address, observed_at, observed_at, score,
                    str(_value(row, "alpha22_tier") or ""),
                    str(_value(row, "alpha22_evidence_state") or ""),
                    str(_value(row, "alpha22_gate_status") or ""),
                    _float(row, "alpha22_sample_size"),
                    str(_value(row, "alpha22_sample_size_source") or ""),
                    _float(row, "alpha22_sample_confidence"), win_rate,
                    _float(row, "alpha22_winrate_wilson_lb"),
                    str(_value(row, "alpha22_cross_token_source") or ""),
                    _float(row, "alpha22_evidence_coverage"), observed_at,
                ),
            )

            cur = conn.execute(
                """
                INSERT OR IGNORE INTO alpha22_wallet_snapshots (
                    address, observed_at, score_version, alpha_score, tier, evidence_state,
                    evidence_coverage, gate_status, sample_size, sample_size_source,
                    sample_confidence, win_rate, winrate_wilson_lb, repeatability,
                    statistical_edge, weighted_cross_token_edge, cross_token_source,
                    profit_quality, temporal_consistency, pnl_roi, closed_positions,
                    total_trades, profit_hhi, largest_win_share, top3_profit_share,
                    median_pnl_per_token, independent_alpha_clusters,
                    independent_cross_token_hits, deep_dive_candidate, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    address, observed_at, SCORE_VERSION, score,
                    str(_value(row, "alpha22_tier") or ""),
                    str(_value(row, "alpha22_evidence_state") or ""),
                    _float(row, "alpha22_evidence_coverage"),
                    str(_value(row, "alpha22_gate_status") or ""),
                    _float(row, "alpha22_sample_size"),
                    str(_value(row, "alpha22_sample_size_source") or ""),
                    _float(row, "alpha22_sample_confidence"), win_rate,
                    _float(row, "alpha22_winrate_wilson_lb"),
                    _float(row, "alpha22_repeatability"),
                    _float(row, "alpha22_statistical_edge"),
                    _float(row, "alpha22_weighted_cross_token_edge"),
                    str(_value(row, "alpha22_cross_token_source") or ""),
                    _float(row, "alpha22_profit_quality"),
                    _float(row, "alpha22_temporal_consistency"),
                    _float(row, "alpha22_pnl_roi"),
                    _float(row, "closed_positions"), _float(row, "total_trades"),
                    _float(row, "profit_hhi"), _float(row, "largest_win_share"),
                    _float(row, "top3_profit_share"), _float(row, "median_pnl_per_token"),
                    _float(row, "independent_alpha_clusters"),
                    _float(row, "independent_cross_token_hits"),
                    1 if bool(_value(row, "alpha22_deep_dive_candidate")) else 0,
                    _payload(row),
                ),
            )
            if cur.rowcount:
                inserted += 1
                conn.execute(
                    """
                    UPDATE alpha22_wallet_state
                    SET last_seen = ?, times_scored = times_scored + 1,
                        last_score = ?, last_tier = ?, last_evidence_state = ?,
                        last_gate_status = ?, last_sample_size = ?, last_sample_source = ?,
                        last_sample_confidence = ?, last_win_rate = ?, last_wilson_lb = ?,
                        last_cross_token_source = ?, last_evidence_coverage = ?, last_updated_at = ?
                    WHERE address = ?
                    """,
                    (
                        observed_at, score, str(_value(row, "alpha22_tier") or ""),
                        str(_value(row, "alpha22_evidence_state") or ""),
                        str(_value(row, "alpha22_gate_status") or ""),
                        _float(row, "alpha22_sample_size"),
                        str(_value(row, "alpha22_sample_size_source") or ""),
                        _float(row, "alpha22_sample_confidence"), win_rate,
                        _float(row, "alpha22_winrate_wilson_lb"),
                        str(_value(row, "alpha22_cross_token_source") or ""),
                        _float(row, "alpha22_evidence_coverage"), observed_at, address,
                    ),
                )

    return {
        "status": "DONE",
        "wallets_seen": int(seen),
        "snapshots_inserted": int(inserted),
        "score_version": SCORE_VERSION,
        "database": str(db_path),
    }
