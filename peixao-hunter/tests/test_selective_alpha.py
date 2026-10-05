from __future__ import annotations

import pandas as pd

from peixao.selective_alpha import selectivity_score, selective_alpha_score, build_selective_stage1


def test_selective_frequency_rewards_few_entries_and_penalizes_hyperactivity():
    selective = selectivity_score(pd.Series({
        "new_positions_per_week": 4,
        "new_positions_per_week_source": "explicit",
        "win_rate": 0.72,
        "closed_positions": 30,
        "median_pnl_per_token": 1800,
        "repeatability_score": 0.75,
    }))
    hyper = selectivity_score(pd.Series({
        "new_positions_per_week": 30,
        "new_positions_per_week_source": "explicit",
        "win_rate": 0.72,
        "closed_positions": 30,
        "median_pnl_per_token": 1800,
        "repeatability_score": 0.75,
    }))
    assert selective["selective_profile"] == "SELECTIVE"
    assert hyper["selective_profile"] == "HYPERACTIVE"
    assert selective["selective_frequency"] > hyper["selective_frequency"]
    assert selective["selective_score"] > hyper["selective_score"]


def test_dune_activity_is_evidence_not_replacement_for_birdeye_winrate():
    result = selectivity_score(pd.Series({
        "dune_new_positions_per_week": 3,
        "win_rate": 0.68,
        "closed_positions": 25,
        "dune_win_rate_30d": 0.90,
        "median_pnl_per_token": 900,
        "dune_repeatability_score": 0.75,
    }))
    assert result["selective_win_rate"] == 0.68
    assert result["selective_win_rate_source"] == "birdeye_or_gmgn"
    assert result["selective_frequency_source"] == "dune_30d_tradeflow_proxy"


def test_selective_deep_dive_requires_wr_gate_and_selectivity():
    good = selective_alpha_score(pd.Series({
        "win_rate": 0.72,
        "closed_positions": 30,
        "new_positions_per_week": 3,
        "new_positions_per_week_source": "explicit",
        "median_pnl_per_token": 2500,
        "repeatability_score": 0.80,
        "weighted_cross_token_score": 0.85,
        "profit_hhi": 0.2,
        "largest_win_share": 0.25,
        "top3_profit_share": 0.5,
        "temporal_consistency_score": 0.8,
        "realized_profit_30d": 30000,
        "realized_roi_30d": 0.45,
    }))
    bad_wr = selective_alpha_score(pd.Series({
        "win_rate": 0.55,
        "closed_positions": 30,
        "new_positions_per_week": 3,
        "median_pnl_per_token": 2500,
        "repeatability_score": 0.80,
        "weighted_cross_token_score": 0.85,
    }))
    assert good["alpha22_gate_status"] == "PASS"
    assert good["selective_alpha_score"] > 60
    assert good["selective_deep_dive_candidate"] is True
    assert bad_wr["alpha22_gate_status"] == "REJECT_WR"
    assert bad_wr["selective_deep_dive_candidate"] is False


def test_stage1_combines_legacy_and_radar_wallets(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    legacy = output / "legacy.csv"
    radar = output / "radar.csv"
    pd.DataFrame([{
        "address": "0xabc", "discovery_score": 80, "gmgn_winrate_30d": 0.66,
        "gmgn_token_num_30d": 12, "realized_profit_30d": 10000,
    }]).to_csv(legacy, index=False)
    pd.DataFrame([{
        "address": "9xQeWvG816bUx9EPjHmaT23yvVM2ZWZx3T4QF3v7FfP", "discovery_score": 90,
        "win_rate": 0.71, "closed_positions": 20, "new_positions_per_week": 3,
        "median_pnl_per_token": 1200, "repeatability_score": 0.75,
        "weighted_cross_token_score": 0.8,
    }]).to_csv(radar, index=False)
    summary = build_selective_stage1(legacy, radar, output, artifact_prefix="TEST")
    frame = pd.read_csv(output / "TEST_wallet_stage1.csv")
    assert summary["wallets"] == 2
    assert set(frame["address"]) == {"0xabc", "9xQeWvG816bUx9EPjHmaT23yvVM2ZWZx3T4QF3v7FfP"}
