from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd


ALPHA_V22_SCORE_VERSION = "V2.2"
ALPHA_V22_WEIGHTS = {
    "repeatability": 25.0,
    "statistical_edge": 20.0,
    "weighted_cross_token_edge": 20.0,
    "profit_quality": 15.0,
    "temporal_consistency": 10.0,
    "pnl_roi": 10.0,
}


def _clamp(value, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        if pd.isna(value):
            return 0.0
        return max(lo, min(hi, float(value)))
    except Exception:
        return 0.0


def _optional_float(row, *keys: str) -> float | None:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        try:
            if pd.isna(value):
                continue
            return float(value)
        except Exception:
            continue
    return None


def _normalize_ratio_or_score(value: float | None) -> float | None:
    if value is None:
        return None
    if value > 1.0:
        return _clamp(value / 100.0)
    return _clamp(value)


def wilson_lower_bound(win_rate: float | None, sample_size: float | int | None, *, z: float = 1.96) -> float:
    if win_rate is None:
        return 0.0
    try:
        n = float(sample_size or 0)
        p = _clamp(win_rate)
    except Exception:
        return 0.0
    if n <= 0:
        return 0.0
    z2 = z * z
    denom = 1.0 + z2 / n
    center = p + z2 / (2.0 * n)
    margin = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * n)) / n)
    return _clamp((center - margin) / denom)


def _sample_size(row) -> tuple[float | None, str, float]:
    """Return sample size, source label and evidence quality.

    closed_positions is preferred. Existing trade/token counts are accepted only
    as clearly-labelled proxies so V2.2 can run on today's data without
    pretending that a proxy is a true count of closed positions.
    """
    closed_positions = _optional_float(row, "closed_positions")
    if closed_positions is not None:
        return max(0.0, closed_positions), "closed_positions", 1.0
    total_trades = _optional_float(row, "total_trades")
    if total_trades is not None:
        return max(0.0, total_trades), "total_trades_proxy", 0.85
    gmgn_proxy = _optional_float(row, "gmgn_token_num_30d")
    if gmgn_proxy is not None:
        return max(0.0, gmgn_proxy), "gmgn_token_num_30d_proxy", 0.55
    return None, "missing", 0.0


def _sample_confidence(n: float | None) -> float:
    if n is None or n <= 0:
        return 0.0
    # Smoothly penalize tiny samples instead of eliminating them.
    return _clamp(1.0 - math.exp(-float(n) / 12.0))


def _gate(win_rate: float | None, sample_size: float | None) -> str:
    if win_rate is None:
        return "DEFER_NO_WINRATE"
    if _clamp(win_rate) < 0.60:
        return "REJECT_WR"
    if sample_size is None or sample_size < 10:
        return "PASS_LOW_SAMPLE"
    return "PASS"


def _component_repeatability(row) -> tuple[float, float] | None:
    repeatability = _normalize_ratio_or_score(_optional_float(row, "repeatability_score"))
    if repeatability is not None:
        return 100.0 * repeatability, 1.0
    wins = _optional_float(row, "winning_tokens")
    traded = _optional_float(row, "tokens_traded")
    if wins is not None and traded is not None and traded > 0:
        return 100.0 * _clamp(wins / traded), 0.90
    return None


def _component_statistical_edge(row) -> tuple[float, float, dict]:
    win_rate = _optional_float(row, "gmgn_winrate_30d", "win_rate")
    sample_size, sample_source, sample_quality = _sample_size(row)
    confidence = _sample_confidence(sample_size)
    wilson = wilson_lower_bound(win_rate, sample_size)
    meta = {
        "alpha22_sample_size": sample_size,
        "alpha22_sample_size_source": sample_source,
        "alpha22_sample_confidence": confidence,
        "alpha22_winrate_wilson_lb": wilson,
        "alpha22_gate_status": _gate(win_rate, sample_size),
    }
    if win_rate is None:
        return 0.0, 0.0, meta
    raw_wr = _clamp((_clamp(win_rate) - 0.50) / 0.30)
    wilson_score = _clamp((wilson - 0.35) / 0.35)
    score = 100.0 * (0.50 * raw_wr + 0.30 * wilson_score + 0.20 * confidence)
    return score, sample_quality, meta


def _component_cross_token(row) -> tuple[float, float, str] | None:
    explicit = _normalize_ratio_or_score(_optional_float(row, "weighted_cross_token_score"))
    if explicit is not None:
        return 100.0 * explicit, 1.0, "weighted_cross_token_score"

    independent_clusters = _optional_float(row, "independent_alpha_clusters")
    if independent_clusters is not None:
        return 100.0 * _clamp(independent_clusters / 5.0), 0.90, "independent_alpha_clusters"

    independent_hits = _optional_float(row, "independent_cross_token_hits")
    if independent_hits is not None:
        return 100.0 * _clamp(independent_hits / 5.0), 0.75, "independent_cross_token_hits"

    distinct_tokens = _optional_float(row, "distinct_tokens")
    if distinct_tokens is not None:
        return 100.0 * _clamp(distinct_tokens / 7.0), 0.35, "distinct_tokens_proxy"
    return None


def _component_profit_quality(row) -> tuple[float, float] | None:
    hhi = _optional_float(row, "profit_hhi")
    largest = _optional_float(row, "largest_win_share")
    top3 = _optional_float(row, "top3_profit_share")
    median_pnl = _optional_float(row, "median_pnl_per_token")
    winning_tokens = _optional_float(row, "winning_tokens")
    closed_tokens = _optional_float(row, "closed_positions", "tokens_traded")

    pieces: list[tuple[float, float]] = []
    if hhi is not None:
        pieces.append((1.0 - _clamp(hhi), 1.0))
    if largest is not None:
        pieces.append((1.0 - _clamp(largest), 0.9))
    if top3 is not None:
        pieces.append((1.0 - _clamp(top3), 0.8))
    if winning_tokens is not None and closed_tokens is not None and closed_tokens > 0:
        pieces.append((_clamp(winning_tokens / closed_tokens), 0.9))
    if median_pnl is not None:
        pieces.append((_clamp(math.log1p(max(0.0, median_pnl)) / math.log1p(5000.0)), 0.7))

    if not pieces:
        return None
    weight = sum(w for _, w in pieces)
    score = sum(v * w for v, w in pieces) / weight
    evidence_quality = _clamp(weight / 4.3)
    return 100.0 * score, evidence_quality


def _component_temporal_consistency(row) -> tuple[float, float] | None:
    explicit = _normalize_ratio_or_score(_optional_float(row, "temporal_consistency_score", "score_stability"))
    if explicit is not None:
        return 100.0 * explicit, 1.0
    positive_weeks = _optional_float(row, "positive_active_weeks")
    active_weeks = _optional_float(row, "active_weeks")
    if positive_weeks is not None and active_weeks is not None and active_weeks > 0:
        return 100.0 * _clamp(positive_weeks / active_weeks), 0.90
    return None


def _component_pnl_roi(row) -> tuple[float, float] | None:
    profit = _optional_float(row, "realized_profit_30d")
    roi = _optional_float(row, "realized_roi_30d")
    if profit is None and roi is None:
        return None
    pieces = []
    if profit is not None:
        pieces.append(_clamp(math.log1p(max(0.0, profit)) / math.log1p(50000.0)))
    if roi is not None:
        pieces.append(_clamp(max(0.0, roi) / 0.50))
    return 100.0 * sum(pieces) / len(pieces), 1.0 if len(pieces) == 2 else 0.70


def alpha_score_v22(row) -> dict:
    """Parallel Stage-1 score focused on future edge, not accumulated PnL.

    Missing evidence remains missing. Proxies are explicitly labelled and carry
    lower evidence quality. The V1 score is not read or altered by this model.
    """
    stat_score, stat_quality, meta = _component_statistical_edge(row)
    components: dict[str, tuple[float, float]] = {}
    if stat_quality > 0:
        components["statistical_edge"] = (stat_score, stat_quality)

    repeatability = _component_repeatability(row)
    if repeatability:
        components["repeatability"] = repeatability

    cross = _component_cross_token(row)
    cross_source = "missing"
    if cross:
        components["weighted_cross_token_edge"] = (cross[0], cross[1])
        cross_source = cross[2]

    profit_quality = _component_profit_quality(row)
    if profit_quality:
        components["profit_quality"] = profit_quality

    temporal = _component_temporal_consistency(row)
    if temporal:
        components["temporal_consistency"] = temporal

    pnl_roi = _component_pnl_roi(row)
    if pnl_roi:
        components["pnl_roi"] = pnl_roi

    weighted_sum = 0.0
    effective_weight = 0.0
    component_out: dict[str, float] = {}
    full_weight = sum(ALPHA_V22_WEIGHTS.values())
    for name, weight in ALPHA_V22_WEIGHTS.items():
        item = components.get(name)
        if item is None:
            component_out[f"alpha22_{name}"] = np.nan
            continue
        score, evidence_quality = item
        ew = weight * _clamp(evidence_quality)
        weighted_sum += score * ew
        effective_weight += ew
        component_out[f"alpha22_{name}"] = round(score, 2)

    coverage = 0.0 if full_weight <= 0 else _clamp(effective_weight / full_weight)
    base_score = np.nan if effective_weight <= 0 else weighted_sum / effective_weight
    gate = meta["alpha22_gate_status"]

    # No final Alpha score without performance evidence. Discovery/cross-token
    # components can still be recorded, but they cannot masquerade as Alpha.
    if gate == "DEFER_NO_WINRATE" or pd.isna(base_score):
        final_score = np.nan
    else:
        final_score = _clamp((float(base_score) / 100.0) * (0.60 + 0.40 * coverage)) * 100.0

    sample_confidence = meta["alpha22_sample_confidence"]
    deep_dive_candidate = bool(
        gate == "PASS"
        and not pd.isna(final_score)
        and final_score >= 60.0
        and coverage >= 0.50
    )

    if pd.isna(final_score):
        tier = "UNENRICHED"
    elif final_score >= 90:
        tier = "S"
    elif final_score >= 80:
        tier = "A+"
    elif final_score >= 70:
        tier = "A"
    elif final_score >= 60:
        tier = "B"
    elif final_score >= 50:
        tier = "C"
    else:
        tier = "D"

    evidence_state = "PROVISIONAL"
    if gate == "PASS" and coverage >= 0.75 and sample_confidence >= 0.80:
        evidence_state = "VALIDATED"
    if gate == "PASS" and coverage >= 0.90 and sample_confidence >= 0.90 and cross_source != "distinct_tokens_proxy":
        evidence_state = "DEEP_VALIDATED"

    return {
        "alpha22_score": np.nan if pd.isna(final_score) else round(float(final_score), 2),
        "alpha22_tier": tier,
        "alpha22_evidence_state": evidence_state if not pd.isna(final_score) else "UNENRICHED",
        "alpha22_evidence_coverage": round(coverage, 4),
        "alpha22_base_score": np.nan if pd.isna(base_score) else round(float(base_score), 2),
        "alpha22_cross_token_source": cross_source,
        "alpha22_deep_dive_candidate": deep_dive_candidate,
        **{k: (round(v, 4) if isinstance(v, float) and not pd.isna(v) else v) for k, v in meta.items()},
        **component_out,
    }


def build_alpha_v22_stage1(
    enriched_queue_path: Path,
    output_dir: Path,
    *,
    max_deep_dive: int = 30,
) -> dict:
    """Build the first V2.2 artifacts without changing any V0/V1 output."""
    stage1_path = output_dir / "V22_wallet_stage1.csv"
    deep_dive_path = output_dir / "V22_deep_dive_queue.csv"

    if not enriched_queue_path.is_file():
        pd.DataFrame().to_csv(stage1_path, index=False)
        pd.DataFrame().to_csv(deep_dive_path, index=False)
        return {"status": "SKIPPED_NO_ENRICHED_QUEUE", "wallets": 0, "scored": 0, "deep_dive_ready": 0}

    try:
        frame = pd.read_csv(enriched_queue_path)
    except pd.errors.EmptyDataError:
        frame = pd.DataFrame()

    if frame.empty:
        frame.to_csv(stage1_path, index=False)
        frame.to_csv(deep_dive_path, index=False)
        return {"status": "DONE", "wallets": 0, "scored": 0, "deep_dive_ready": 0}

    alpha = frame.apply(lambda row: pd.Series(alpha_score_v22(row)), axis=1)
    out = pd.concat([frame, alpha], axis=1)
    out = out.sort_values(
        ["alpha22_score", "alpha22_evidence_coverage", "discovery_score"],
        ascending=[False, False, False],
        na_position="last",
        kind="mergesort",
    ).reset_index(drop=True)
    out.to_csv(stage1_path, index=False)

    deep = out[out["alpha22_deep_dive_candidate"].eq(True)].head(int(max_deep_dive)).copy()
    deep.to_csv(deep_dive_path, index=False)

    return {
        "status": "DONE",
        "wallets": int(len(out)),
        "scored": int(out["alpha22_score"].notna().sum()),
        "pass": int((out["alpha22_gate_status"] == "PASS").sum()),
        "pass_low_sample": int((out["alpha22_gate_status"] == "PASS_LOW_SAMPLE").sum()),
        "reject_wr": int((out["alpha22_gate_status"] == "REJECT_WR").sum()),
        "defer_no_winrate": int((out["alpha22_gate_status"] == "DEFER_NO_WINRATE").sum()),
        "deep_dive_ready": int(len(deep)),
        "stage1_output": str(stage1_path),
        "deep_dive_output": str(deep_dive_path),
    }
