from __future__ import annotations

import pandas as pd

from peixao.alpha_v22 import alpha_score_v22, build_alpha_v22_stage1


def test_sample_confidence_penalizes_tiny_sample_without_rejecting():
    tiny = alpha_score_v22(pd.Series({
        "gmgn_winrate_30d": 1.0,
        "closed_positions": 2,
        "weighted_cross_token_score": 0.8,
        "repeatability_score": 0.8,
    }))
    mature = alpha_score_v22(pd.Series({
        "gmgn_winrate_30d": 0.67,
        "closed_positions": 80,
        "weighted_cross_token_score": 0.8,
        "repeatability_score": 0.8,
    }))

    assert tiny["alpha22_gate_status"] == "PASS_LOW_SAMPLE"
    assert mature["alpha22_gate_status"] == "PASS"
    assert tiny["alpha22_sample_confidence"] < mature["alpha22_sample_confidence"]
    assert tiny["alpha22_deep_dive_candidate"] is False


def test_cross_token_proxy_is_explicit_and_lower_evidence():
    proxy = alpha_score_v22(pd.Series({
        "gmgn_winrate_30d": 0.70,
        "closed_positions": 20,
        "distinct_tokens": 7,
    }))
    independent = alpha_score_v22(pd.Series({
        "gmgn_winrate_30d": 0.70,
        "closed_positions": 20,
        "independent_alpha_clusters": 5,
    }))

    assert proxy["alpha22_cross_token_source"] == "distinct_tokens_proxy"
    assert independent["alpha22_cross_token_source"] == "independent_alpha_clusters"
    assert proxy["alpha22_evidence_coverage"] < independent["alpha22_evidence_coverage"]


def test_missing_profit_concentration_is_not_invented():
    result = alpha_score_v22(pd.Series({
        "gmgn_winrate_30d": 0.70,
        "closed_positions": 20,
        "independent_alpha_clusters": 3,
    }))
    assert pd.isna(result["alpha22_profit_quality"])
    assert pd.isna(result["alpha22_repeatability"])
    assert pd.isna(result["alpha22_temporal_consistency"])


def test_stage1_keeps_all_wallets_and_only_scores_supported_rows(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    enriched = output / "V6_wallet_queue_enriched.csv"
    pd.DataFrame([
        {
            "address": "0x1",
            "discovery_score": 90,
            "gmgn_winrate_30d": 0.72,
            "gmgn_token_num_30d": 30,
            "distinct_tokens": 5,
            "realized_profit_30d": 10000,
            "realized_roi_30d": 0.4,
        },
        {
            "address": "0x2",
            "discovery_score": 80,
            "gmgn_winrate_30d": 0.50,
            "gmgn_token_num_30d": 30,
            "distinct_tokens": 4,
        },
        {
            "address": "0x3",
            "discovery_score": 70,
            "distinct_tokens": 3,
        },
    ]).to_csv(enriched, index=False)

    summary = build_alpha_v22_stage1(enriched, output, max_deep_dive=30)
    frame = pd.read_csv(output / "V22_wallet_stage1.csv")

    assert summary["wallets"] == 3
    assert len(frame) == 3
    assert summary["scored"] == 2
    assert summary["reject_wr"] == 1
    row3 = frame[frame["address"].eq("0x3")].iloc[0]
    assert pd.isna(row3["alpha22_score"])
