from __future__ import annotations

from pathlib import Path
import json
import sqlite3

import pandas as pd


def _val(row: dict, key: str):
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
    value = _val(row, key)
    try:
        return None if value is None else float(value)
    except Exception:
        return None


def _payload(row: dict) -> str:
    out = {}
    for key in row:
        value = _val(row, key)
        out[str(key)] = value if isinstance(value, (str, int, float, bool)) or value is None else str(value)
    return json.dumps(out, ensure_ascii=False, sort_keys=True)


def record_token_signal_snapshots(frame: pd.DataFrame, db_path: Path) -> dict:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA busy_timeout = 5000")
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_token_signal_time ON token_signal_snapshots(chain, token_address, observed_at)")
        inserted = 0
        for row in frame.to_dict("records"):
            address = str(row.get("token_address", "") or "").strip()
            observed_at = str(row.get("observed_at", "") or "").strip()
            if not address or not observed_at:
                continue
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO token_signal_snapshots (
                    chain, token_address, observed_at, source, symbol, price_usd,
                    market_cap_usd, liquidity_usd, volume_24h_usd, token_age_seconds,
                    smart_buys, smart_sells, holders, snipers, bundlers, risk_score,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(row.get("chain", "solana") or "solana"), address, observed_at,
                    str(row.get("radar_source", "TOKEN_RADAR") or "TOKEN_RADAR"),
                    _val(row, "symbol"), _float(row, "price_usd"), _float(row, "market_cap_usd"),
                    _float(row, "liquidity_usd"), _float(row, "volume_24h_usd"), _float(row, "token_age_seconds"),
                    _float(row, "smart_buys"), _float(row, "smart_sells"), _float(row, "holders"),
                    _float(row, "snipers"), _float(row, "bundlers"), _float(row, "risk_score"), _payload(row),
                ),
            )
            inserted += int(bool(cur.rowcount))
    return {"status": "DONE", "snapshots_inserted": inserted, "database": str(db_path)}
