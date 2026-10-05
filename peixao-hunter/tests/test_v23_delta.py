from pathlib import Path

import pandas as pd

from peixao.adaptive_queue import adaptive_provider_limit, evidence_gap_score
from peixao.chain_aware_inputs import build_chain_aware_inputs
from peixao.evidence_ledger import canonical_metrics_for_wallet, normalize_metrics, record_metrics, record_provider_run
from peixao.zerion_evm import _parse_pnl


def test_nansen_units_are_canonical_ratios():
    metrics, issues = normalize_metrics("NANSEN", {
        "win_rate": 0.64,
        "realized_roi_30d": 317.0,
        "closed_positions": 12,
    })
    assert metrics["win_rate"] == 0.64
    assert metrics["realized_roi_30d"] == 3.17
    assert metrics["closed_positions"] == 12
    assert "realized_roi_30d:nansen_percent_points_to_ratio" in issues


def test_nansen_rejects_ambiguous_winrate_percent_points():
    metrics, issues = normalize_metrics("NANSEN", {"win_rate": 64.0})
    assert "win_rate" not in metrics
    assert "win_rate:invalid_ratio_unit" in issues


def test_zerion_percentage_parser_converts_small_percent_correctly():
    payload = {
        "data": {
            "attributes": {
                "realized_gain": 100.0,
                "relative_realized_gain_percentage": 2.0,
                "breakdown": {
                    "by_id": {
                        "a": {"realized_gain": 100.0, "realized_cost_basis": 5000.0}
                    }
                },
            }
        }
    }
    metrics = _parse_pnl(payload, chain="base")
    assert metrics["realized_roi_30d"] == 0.02
    assert metrics["zerion_roi_unit"] == "ratio"


def test_chain_address_identity_is_preserved(tmp_path: Path):
    address = "0x1111111111111111111111111111111111111111"
    base = tmp_path / "base.csv"
    robinhood = tmp_path / "robinhood.csv"
    output = tmp_path / "canonical.csv"
    db = tmp_path / "db.sqlite3"
    pd.DataFrame([{"address": address, "chain": "base", "discovery_score": 80}]).to_csv(base, index=False)
    pd.DataFrame([{"address": address, "chain": "robinhood", "discovery_score": 70}]).to_csv(robinhood, index=False)
    result = build_chain_aware_inputs(
        legacy_path=None,
        solana_path=None,
        base_path=base,
        robinhood_path=robinhood,
        output_path=output,
        db_path=db,
    )
    frame = pd.read_csv(output)
    assert result["wallets"] == 2
    assert set(frame["chain"]) == {"base", "robinhood"}
    assert frame["wallet_key"].nunique() == 2


def test_evidence_ledger_keeps_provider_disagreement(tmp_path: Path):
    db = tmp_path / "db.sqlite3"
    address = "0x2222222222222222222222222222222222222222"
    record_metrics(db, chain="base", address=address, provider="NANSEN", metrics={"win_rate": 0.64})
    record_metrics(db, chain="base", address=address, provider="ZERION", metrics={"win_rate": 0.58})
    canonical = canonical_metrics_for_wallet(db, "base", address)
    assert canonical["win_rate"] == 0.64
    assert canonical["evidence_source_count"] == 2
    assert round(canonical["evidence_disagreement_wr"], 2) == 0.06
    assert canonical["evidence_confidence"] < 1.0


def test_evidence_gap_prioritizes_missing_winrate():
    missing_wr = evidence_gap_score({
        "closed_positions": 15,
        "realized_profit_30d": 10,
        "repeatability_score": 0.7,
        "new_positions_per_week": 2,
    })
    only_minor = evidence_gap_score({
        "win_rate": 0.7,
        "closed_positions": 15,
        "realized_profit_30d": 10,
        "repeatability_score": 0.7,
    })
    assert missing_wr > only_minor
    assert missing_wr >= 50


def test_provider_throttle_reduces_next_batch(tmp_path: Path):
    db = tmp_path / "db.sqlite3"
    record_provider_run(
        db,
        provider="NANSEN",
        chain="base",
        attempted=10,
        enriched=8,
        errors=2,
        http_calls=20,
        credits=20,
        statuses=[200, 429],
    )
    assert adaptive_provider_limit(db, "NANSEN", "base", 20, backlog=100) == 10
