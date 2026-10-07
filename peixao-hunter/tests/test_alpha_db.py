import sqlite3
import pandas as pd
from peixao.alpha_db import initialize_alpha_db, record_alpha22_stage1_csv


def test_alpha_db_tables(tmp_path):
    db = tmp_path / "peixao.sqlite3"
    initialize_alpha_db(db)
    with sqlite3.connect(db) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"alpha22_wallet_state", "alpha22_wallet_snapshots", "token_signal_snapshots"}.issubset(tables)


def test_alpha_snapshot_idempotent(tmp_path):
    db = tmp_path / "peixao.sqlite3"
    csv_path = tmp_path / "stage1.csv"
    pd.DataFrame([{
        "address": "wallet-test",
        "alpha22_score": 71.5,
        "alpha22_tier": "A",
        "alpha22_evidence_state": "PROVISIONAL",
        "alpha22_evidence_coverage": 0.61,
        "alpha22_gate_status": "PASS",
        "alpha22_sample_size": 22,
        "alpha22_sample_size_source": "closed_positions",
        "alpha22_sample_confidence": 0.84,
        "gmgn_winrate_30d": 0.67,
        "alpha22_winrate_wilson_lb": 0.48,
        "alpha22_cross_token_source": "independent_alpha_clusters",
        "alpha22_deep_dive_candidate": True,
    }]).to_csv(csv_path, index=False)
    stamp = "2026-10-04T13:30:00Z"
    first = record_alpha22_stage1_csv(csv_path, db, observed_at=stamp)
    second = record_alpha22_stage1_csv(csv_path, db, observed_at=stamp)
    assert first["snapshots_inserted"] == 1
    assert second["snapshots_inserted"] == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM alpha22_wallet_snapshots").fetchone()[0] == 1
