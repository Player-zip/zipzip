from __future__ import annotations

import pandas as pd

from peixao.quicknode_solana import (
    QuickNodeCreditBudget,
    _wallet_tx_metrics,
    merge_quicknode_with_birdeye,
)


def test_quicknode_credit_budget_caps_run_and_day(tmp_path):
    budget = QuickNodeCreditBudget(tmp_path / "budget.json", daily_limit=90, run_limit=60)
    assert budget.reserve(30) is True
    assert budget.reserve(30) is True
    assert budget.reserve(30) is False
    summary = budget.summary()
    assert summary["spent_run"] == 60
    assert summary["spent_today"] == 60
    assert summary["remaining_run"] == 0


def test_wallet_tx_metrics_reads_owner_token_deltas():
    wallet = "Wallet111"
    signatures = [{"signature": "a", "err": None}, {"signature": "b", "err": {"x": 1}}]
    transactions = [{
        "blockTime": 1700000000,
        "meta": {
            "preTokenBalances": [{
                "owner": wallet,
                "mint": "MintA",
                "uiTokenAmount": {"amount": "1000000", "decimals": 6},
            }],
            "postTokenBalances": [{
                "owner": wallet,
                "mint": "MintA",
                "uiTokenAmount": {"amount": "2500000", "decimals": 6},
            }],
        },
    }]
    out = _wallet_tx_metrics(wallet, signatures, transactions)
    assert out["quicknode_recent_signatures"] == 2
    assert out["quicknode_recent_successful_txs"] == 1
    assert out["quicknode_buy_events"] == 1
    assert out["quicknode_sell_events"] == 0
    assert out["quicknode_unique_token_mints"] == 1


def test_merge_keeps_birdeye_performance_and_adds_quicknode_evidence(tmp_path):
    birdeye_path = tmp_path / "birdeye.csv"
    quicknode_path = tmp_path / "quicknode.csv"
    output_path = tmp_path / "merged.csv"

    pd.DataFrame([{
        "address": "WalletA",
        "win_rate": 0.72,
        "realized_profit_30d": 1234.0,
        "cross_token_hits": 2,
        "candidate_sources": "birdeye",
    }]).to_csv(birdeye_path, index=False)

    pd.DataFrame([{
        "address": "WalletA",
        "cross_token_hits": 3,
        "quicknode_activity_score": 88.0,
        "candidate_sources": "quicknode",
    }, {
        "address": "WalletB",
        "cross_token_hits": 1,
        "quicknode_activity_score": 55.0,
        "candidate_sources": "quicknode",
    }]).to_csv(quicknode_path, index=False)

    result = merge_quicknode_with_birdeye(birdeye_path, quicknode_path, output_path)
    merged = pd.read_csv(output_path).set_index("address")

    assert result["wallets"] == 2
    assert result["cross_provider_wallets"] == 1
    assert merged.loc["WalletA", "win_rate"] == 0.72
    assert merged.loc["WalletA", "cross_token_hits"] == 3
    assert merged.loc["WalletA", "quicknode_activity_score"] == 88.0
    assert bool(merged.loc["WalletA", "quicknode_cross_provider_confirmed"]) is True
    assert pd.isna(merged.loc["WalletB", "win_rate"])
