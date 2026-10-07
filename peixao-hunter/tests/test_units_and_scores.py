import pandas as pd

from peixao.alpha_v22 import alpha_score_v22
from peixao.birdeye_alpha import _parse_pnl
from peixao.evidence_ledger import normalize_metrics
from peixao.nansen_evm import _normalized_metrics, upgrade_cached_metrics
from peixao.selective_alpha import selective_alpha_score
from peixao.units import ratio_from_any, strict_ratio


def test_ratio_from_any_normalizes_and_never_clamps():
    assert ratio_from_any(0.55) == 0.55
    assert ratio_from_any(55) == 0.55
    assert ratio_from_any(1.0) == 1.0
    assert ratio_from_any(250) is None
    assert ratio_from_any(-0.1) is None
    assert strict_ratio(64.0) is None


def _row(win_rate):
    return pd.Series({
        "address": "0xabc", "chain": "base", "win_rate": win_rate, "gmgn_winrate_30d": win_rate,
        "closed_positions": 25, "realized_profit_30d": 4000.0, "realized_roi_30d": 0.125,
        "repeatability_score": 0.7, "new_positions_per_week": 3.0, "median_pnl_per_token": 150.0,
    })


def test_percent_win_rate_does_not_pass_the_60pct_gate():
    as_percent = alpha_score_v22(_row(55.0))
    as_ratio = alpha_score_v22(_row(0.55))
    assert as_percent["alpha22_gate_status"] == "REJECT_WR"
    assert as_percent["alpha22_score"] == as_ratio["alpha22_score"]
    selective = selective_alpha_score(_row(55.0))
    assert selective["selective_win_rate"] == 0.55
    assert selective["selective_deep_dive_candidate"] is False


def test_nansen_adapter_emits_roi_as_ratio_with_unit_marker():
    metrics = _normalized_metrics({"win_rate": 0.64, "realized_pnl_percent": 12.5}, [], chain="base")
    assert metrics["realized_roi_30d"] == 0.125
    assert metrics["realized_roi_unit"] == "ratio"
    assert metrics["nansen_roi_raw_percent"] == 12.5
    assert _normalized_metrics({"win_rate": 64.0}, [], chain="base")["win_rate"] is None


def test_legacy_nansen_cache_entries_are_upgraded_once():
    legacy = {"nansen_evidence": True, "realized_roi_30d": 12.5}
    upgraded = upgrade_cached_metrics(legacy)
    assert upgraded["realized_roi_30d"] == 0.125
    assert upgraded["realized_roi_unit"] == "ratio"
    assert upgrade_cached_metrics(upgraded) is upgraded
    assert legacy["realized_roi_30d"] == 12.5


def test_birdeye_roi_unit_comes_from_field_name_not_magnitude():
    percent = _parse_pnl({"data": {"summary": {"win_rate": 70, "realized_profit_percent": 600}}}, detail_status="OK")
    assert percent["win_rate"] == 0.70
    assert percent["realized_roi_30d"] == 6.0
    ratio = _parse_pnl({"data": {"summary": {"realized_roi": 6.0}}}, detail_status="OK")
    assert ratio["realized_roi_30d"] == 6.0
    small = _parse_pnl({"data": {"summary": {"realized_profit_percent": 3.0}}}, detail_status="OK")
    assert small["realized_roi_30d"] == 0.03


def test_ledger_trusts_unit_marker_and_keeps_gmgn_ratio():
    marked, _ = normalize_metrics("NANSEN", {"realized_roi_30d": 0.125, "realized_roi_unit": "ratio"})
    assert marked["realized_roi_30d"] == 0.125
    legacy, issues = normalize_metrics("NANSEN", {"realized_roi_30d": 317.0})
    assert legacy["realized_roi_30d"] == 3.17
    assert "realized_roi_30d:nansen_percent_points_to_ratio" in issues
    gmgn, _ = normalize_metrics("GMGN_CACHE", {"realized_roi_30d": 3.0})
    assert gmgn["realized_roi_30d"] == 3.0
