from __future__ import annotations

import pandas as pd

from peixao.dune_selectivity import _sql, enrich_stage1_with_dune


def test_dune_sql_is_batched_and_scoped_to_recent_wallet_trades():
    wallets = [
        "7YttLkHDoJfw7B3NsYWjJzUhNPc6Q8ALq6bB6pYqpump",
        "9xQeWvG816bUx9EPjHmaT23yvVM2ZWZx3T4QF3v7FfP",
    ]
    sql = _sql(wallets, 30)
    assert "dex_solana.trades" in sql
    assert "CURRENT_DATE - INTERVAL '30' DAY" in sql
    assert wallets[0] in sql and wallets[1] in sql
    assert "new_positions_per_week" in sql
    assert "dune_repeatability_score" in sql


def test_dune_skips_network_when_stage1_has_no_solana_wallets(tmp_path, monkeypatch):
    stage1 = tmp_path / "stage1.csv"
    pd.DataFrame([
        {"address": "0xabc", "alpha22_score": 75, "discovery_score": 90},
        {"address": "0xdef", "alpha22_score": 70, "discovery_score": 80},
    ]).to_csv(stage1, index=False)
    called = {"n": 0}

    def fail_request(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("Dune should not be called for non-Solana wallets")

    monkeypatch.setattr("peixao.dune_selectivity.requests.post", fail_request)
    monkeypatch.setattr("peixao.dune_selectivity.requests.get", fail_request)
    result = enrich_stage1_with_dune(
        stage1,
        tmp_path / "output",
        tmp_path / "state",
        api_key="secret",
        enabled=True,
    )
    assert result["status"] == "DONE_NO_SOLANA_WALLETS"
    assert result["dune_executions"] == 0
    assert called["n"] == 0
