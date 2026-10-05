from peixao.scoring import (
    discovery_components,
    discovery_priority,
    peixao_score_v0,
    peixao_score_v1,
    sample_confidence,
    wilson_lower_bound,
    win_rate_gate,
)


def test_discovery_score_tx_from_eoa_matches_v6_formula():
    row = {"distinct_tokens": 7, "sampled_txs": 7, "source": "TX_FROM", "address_type": "EOA_NO_CODE"}
    parts = discovery_components(row)
    assert parts["discovery_score"] == 97.8
    assert discovery_priority(parts["discovery_score"]) == "P0"


def test_wash_trader_penalty_is_preserved():
    row = {
        "discovery_score": 80,
        "gmgn_token_num_30d": 100,
        "gmgn_winrate_30d": 0.5,
        "realized_profit_30d": 10000,
        "realized_roi_30d": 0.3,
        "gmgn_gt_5x": 2,
        "gmgn_tags": "wash_trader,gmgn",
    }
    score = peixao_score_v0(row)
    assert "wash_trader" in score["risk_flags"]
    assert score["risk_penalty"] >= 25


def test_v1_keeps_60_percent_winrate_as_hard_floor():
    assert win_rate_gate(0.5999, 100) == "REJECT_WR"
    assert win_rate_gate(0.60, 100) == "PASS"


def test_v1_confidence_rewards_real_sample_size():
    assert sample_confidence(2) == 0.25
    assert sample_confidence(10) == 1.0
    assert wilson_lower_bound(0.70, 100) > wilson_lower_bound(0.70, 10)


def test_v1_is_provisional_when_deep_features_are_missing():
    row = {
        "discovery_score": 90,
        "distinct_tokens": 5,
        "gmgn_token_num_30d": 30,
        "gmgn_winrate_30d": 0.70,
        "realized_profit_30d": 25000,
        "realized_roi_30d": 0.40,
        "gmgn_tags": "smart_money",
    }
    score = peixao_score_v1(row)
    assert score["v1_gate_status"] == "PASS"
    assert score["evidence_state"] == "PROVISIONAL"
    assert score["evidence_coverage"] < 0.65
    assert "cross_token_proxy" in score["v1_risk_flags"]


def test_v1_hard_rejects_insider_but_not_sniper():
    base = {
        "distinct_tokens": 6,
        "gmgn_token_num_30d": 40,
        "gmgn_winrate_30d": 0.75,
        "realized_profit_30d": 40000,
        "realized_roi_30d": 0.45,
    }
    insider = peixao_score_v1({**base, "gmgn_tags": "insider"})
    sniper = peixao_score_v1({**base, "gmgn_tags": "sniper"})

    assert insider["v1_gate_status"] == "REJECT_RISK"
    assert insider["peixao_score_v1"] == 0.0
    assert sniper["v1_gate_status"] == "PASS"
    assert sniper["peixao_score_v1"] > 0.0
    assert "sniper_review" in sniper["v1_risk_flags"]
