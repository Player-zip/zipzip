import sqlite3

import pandas as pd

from peixao.wallet_db import record_wallet_snapshots


def _ranked_frame(score=82.5):
    return pd.DataFrame([
        {
            "address": "0xABC",
            "peixao_score_v1": score,
            "peixao_tier_v1": "A+",
            "evidence_state": "PROVISIONAL",
            "evidence_coverage": 0.45,
            "sample_confidence": 1.0,
            "v1_gate_status": "PASS",
            "gmgn_winrate_30d": 0.72,
            "gmgn_token_num_30d": 30,
            "realized_profit_30d": 12000,
            "realized_roi_30d": 0.35,
            "distinct_tokens": 5,
            "v1_risk_flags": "cross_token_proxy",
        }
    ])


def test_master_wallet_db_keeps_history_and_is_idempotent(tmp_path):
    db_path = tmp_path / "peixao_master.sqlite3"

    first = record_wallet_snapshots(_ranked_frame(), db_path, observed_at="2026-10-04T10:00:00Z")
    duplicate = record_wallet_snapshots(_ranked_frame(), db_path, observed_at="2026-10-04T10:00:00Z")
    second = record_wallet_snapshots(_ranked_frame(score=86.0), db_path, observed_at="2026-10-05T10:00:00Z")

    assert first["snapshots_inserted"] == 1
    assert duplicate["snapshots_inserted"] == 0
    assert second["snapshots_inserted"] == 1

    with sqlite3.connect(db_path) as conn:
        wallet = conn.execute(
            "SELECT address, times_spotted, last_score, first_seen, last_seen FROM wallets"
        ).fetchone()
        snapshots = conn.execute(
            "SELECT COUNT(*), MIN(peixao_score), MAX(peixao_score) FROM wallet_snapshots"
        ).fetchone()

    assert wallet == ("0xabc", 2, 86.0, "2026-10-04T10:00:00Z", "2026-10-05T10:00:00Z")
    assert snapshots == (2, 82.5, 86.0)
