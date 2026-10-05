from __future__ import annotations

import math
import re
import numpy as np
import pandas as pd


PEIXAO_SCORE_V1_WEIGHTS = {
    "statistical_edge": 20.0,
    "independent_cross_token_edge": 20.0,
    "repeatability": 15.0,
    "profit_quality": 15.0,
    "profit_diversity": 10.0,
    "stability": 10.0,
    "copyability": 10.0,
}

HARD_REJECT_TAGS = {
    "dev",
    "developer",
    "bundler",
    "insider",
    "chef",
    "known_deployer",
    "deployer",
    "airdrop_only",
}


def discovery_components(row) -> dict[str, float]:
    tokens = max(0.0, float(row.get("distinct_tokens", 0) or 0))
    txs = max(0.0, float(row.get("sampled_txs", 0) or 0))
    source = str(row.get("source", "")).upper()
    address_type = str(row.get("address_type", "")).upper()
    recurrence = 55.0 * min(tokens / 7.0, 1.0)
    source_pts = 25.0 if source == "TX_FROM" else 10.0
    type_pts = 15.0 if address_type == "EOA_NO_CODE" else (5.0 if address_type in ("UNKNOWN", "UNCLASSIFIED", "") else 0.0)
    activity = 5.0 * min(math.log1p(txs) / math.log1p(40.0), 1.0)
    return {
        "score_recurrence": round(recurrence, 2),
        "score_source": round(source_pts, 2),
        "score_address_type": round(type_pts, 2),
        "score_activity": round(activity, 2),
        "discovery_score": round(recurrence + source_pts + type_pts + activity, 2),
    }


def discovery_priority(score: float) -> str:
    if score >= 85:
        return "P0"
    if score >= 70:
        return "P1"
    if score >= 55:
        return "P2"
    return "P3"


def _clamp(value, lo=0.0, hi=1.0) -> float:
    try:
        if pd.isna(value):
            return 0.0
        return max(lo, min(hi, float(value)))
    except Exception:
        return 0.0


def _optional_float(row, key: str) -> float | None:
    value = row.get(key)
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def _normalize_ratio_or_score(value: float | None) -> float | None:
    if value is None:
        return None
    if value > 1.0:
        return _clamp(value / 100.0)
    return _clamp(value)


def _normalized_tags(value) -> set[str]:
    raw = str(value or "").strip().lower()
    if not raw or raw == "nan":
        return set()
    return {part for part in re.split(r"[^a-z0-9_]+", raw) if part}


def sample_confidence(sample_size: float | int | None) -> float:
    """Cheap confidence multiplier kept intentionally conservative for V1.

    The table is explicit and easy to backtest. It can later be replaced by a
    calibrated Bayesian posterior without changing the rest of the score API.
    """
    try:
        n = int(max(0, float(sample_size or 0)))
    except Exception:
        n = 0
    if n <= 0:
        return 0.0
    if n == 1:
        return 0.10
    if n == 2:
        return 0.25
    if n == 3:
        return 0.45
    if n == 4:
        return 0.65
    if n == 5:
        return 0.80
    if n <= 9:
        return 0.90
    return 1.00


def wilson_lower_bound(win_rate: float | None, sample_size: float | int | None, *, z: float = 1.96) -> float:
    """95% Wilson lower confidence bound for a binomial win rate."""
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


def win_rate_gate(win_rate: float | None, sample_size: float | int | None) -> str:
    """Operational cheap gate: WR <60% is rejected; tiny samples are deferred."""
    if win_rate is None:
        return "DEFER_NO_WINRATE"
    if _clamp(win_rate) < 0.60:
        return "REJECT_WR"
    try:
        n = int(max(0, float(sample_size or 0)))
    except Exception:
        n = 0
    if n < 5:
        return "DEFER_SAMPLE"
    return "PASS"


def peixao_score_v0(row) -> dict:
    if pd.isna(row.get("gmgn_token_num_30d")):
        return {"peixao_score_v0": np.nan, "peixao_tier": "UNENRICHED", "risk_penalty": 0.0, "risk_flags": ""}

    discovery = _clamp(row.get("discovery_score", 0) / 100.0)
    win = _clamp(row.get("gmgn_winrate_30d", 0) / 0.70)
    profit30 = float(row.get("realized_profit_30d", 0) or 0)
    profit_score = _clamp(math.log1p(max(0.0, profit30)) / math.log1p(50000.0))
    roi30_raw = row.get("realized_roi_30d", 0)
    roi30 = 0.0 if pd.isna(roi30_raw) else max(0.0, float(roi30_raw or 0))
    roi_score = _clamp(roi30 / 0.50)
    gt5 = _clamp(float(row.get("gmgn_gt_5x", 0) or 0) / 3.0)
    activity = _clamp(math.log1p(max(0.0, float(row.get("gmgn_token_num_30d", 0) or 0))) / math.log1p(500.0))

    base = 40 * discovery + 20 * win + 15 * profit_score + 10 * roi_score + 10 * gt5 + 5 * activity
    tags = str(row.get("gmgn_tags", "") or "").lower()
    penalty = 0.0
    flags = []
    if "sandwich_bot" in tags:
        penalty += 35.0
        flags.append("sandwich_bot")
    if "wash_trader" in tags:
        penalty += 25.0
        flags.append("wash_trader")
    if profit30 < 0:
        penalty += min(10.0, 2.0 + 2.0 * math.log10(1.0 + abs(profit30)))
        flags.append("negative_pnl_30d")
    winrate = float(row.get("gmgn_winrate_30d", 0) or 0)
    if winrate < 0.20 and float(row.get("gmgn_token_num_30d", 0) or 0) >= 20:
        penalty += 5.0
        flags.append("low_winrate_30d")

    score = max(0.0, min(100.0, base - penalty))
    tier = "A+" if score >= 80 else "A" if score >= 70 else "B" if score >= 60 else "C" if score >= 50 else "D"
    return {
        "peixao_score_v0": round(score, 2),
        "peixao_tier": tier,
        "risk_penalty": round(penalty, 2),
        "risk_flags": ",".join(flags),
    }


def peixao_score_v1(row) -> dict:
    """Evidence-aware wallet score that can grow as deeper features arrive.

    V1 deliberately keeps V0 untouched. Missing deep-dive features do not get
    invented values: they lower evidence coverage and keep the wallet marked as
    PROVISIONAL. This lets the current worker produce a useful cheap score today
    while future funding/clustering/concentration/copyability data can plug in
    without another scoring rewrite.
    """
    sample_size = _optional_float(row, "gmgn_token_num_30d")
    if sample_size is None:
        return {
            "peixao_score_v1": np.nan,
            "peixao_tier_v1": "UNENRICHED",
            "evidence_state": "UNENRICHED",
            "evidence_coverage": 0.0,
            "sample_confidence": 0.0,
            "winrate_wilson_lb": np.nan,
            "v1_gate_status": "DEFER_NO_WINRATE",
            "v1_integrity_factor": 1.0,
            "v1_risk_factor": 1.0,
            "v1_risk_flags": "",
        }

    winrate = _optional_float(row, "gmgn_winrate_30d")
    confidence = sample_confidence(sample_size)
    gate = win_rate_gate(winrate, sample_size)
    wilson_lb = wilson_lower_bound(winrate, sample_size)

    tags = _normalized_tags(row.get("gmgn_tags", ""))
    flags: list[str] = []
    hard_hits = sorted(tags.intersection(HARD_REJECT_TAGS))
    integrity_factor = 1.0
    if hard_hits:
        integrity_factor = 0.0
        flags.extend(f"hard_reject:{tag}" for tag in hard_hits)
        gate = "REJECT_RISK"

    risk_factor = 1.0
    if "sandwich_bot" in tags:
        risk_factor *= 0.50
        flags.append("sandwich_bot")
    if "wash_trader" in tags:
        risk_factor *= 0.65
        flags.append("wash_trader")
    if "sniper" in tags:
        flags.append("sniper_review")
    if "bot" in tags and "sandwich_bot" not in tags:
        flags.append("bot_review")

    profit30 = _optional_float(row, "realized_profit_30d")
    roi30 = _optional_float(row, "realized_roi_30d")
    if profit30 is not None and profit30 < 0:
        risk_factor *= 0.90
        flags.append("negative_pnl_30d")

    components: dict[str, tuple[float, float]] = {}

    if winrate is not None:
        raw_wr = 100.0 * _clamp((_clamp(winrate) - 0.50) / 0.30)
        lb_score = 100.0 * _clamp((wilson_lb - 0.40) / 0.30)
        components["statistical_edge"] = (0.70 * raw_wr + 0.30 * lb_score, 1.0)

    independent_hits = _optional_float(row, "independent_cross_token_hits")
    if independent_hits is not None:
        components["independent_cross_token_edge"] = (100.0 * _clamp(independent_hits / 5.0), 1.0)
    else:
        distinct_tokens = _optional_float(row, "distinct_tokens")
        if distinct_tokens is not None:
            components["independent_cross_token_edge"] = (100.0 * _clamp(distinct_tokens / 7.0), 0.50)
            flags.append("cross_token_proxy")

    repeatability = _normalize_ratio_or_score(_optional_float(row, "repeatability_score"))
    if repeatability is not None:
        components["repeatability"] = (100.0 * repeatability, 1.0)
    else:
        wins = _optional_float(row, "winning_tokens")
        traded = _optional_float(row, "tokens_traded")
        if wins is not None and traded is not None and traded > 0:
            components["repeatability"] = (100.0 * _clamp(wins / traded), 1.0)

    if profit30 is not None or roi30 is not None:
        profit_score = 0.0 if profit30 is None else 100.0 * _clamp(math.log1p(max(0.0, profit30)) / math.log1p(50000.0))
        roi_score = 0.0 if roi30 is None else 100.0 * _clamp(max(0.0, roi30) / 0.50)
        if profit30 is not None and roi30 is not None:
            components["profit_quality"] = (0.60 * profit_score + 0.40 * roi_score, 1.0)
        else:
            components["profit_quality"] = (profit_score if profit30 is not None else roi_score, 0.70)

    profit_hhi = _optional_float(row, "profit_hhi")
    largest_win_share = _optional_float(row, "largest_win_share")
    top3_profit_share = _optional_float(row, "top3_profit_share")
    if profit_hhi is not None:
        components["profit_diversity"] = (100.0 * (1.0 - _clamp(profit_hhi)), 1.0)
    elif largest_win_share is not None:
        diversity = 1.0 - _clamp(largest_win_share)
        if top3_profit_share is not None:
            diversity = 0.60 * diversity + 0.40 * (1.0 - _clamp(top3_profit_share))
        components["profit_diversity"] = (100.0 * _clamp(diversity), 0.85)

    stability = _normalize_ratio_or_score(_optional_float(row, "score_stability"))
    if stability is not None:
        components["stability"] = (100.0 * stability, 1.0)
    else:
        positive_weeks = _optional_float(row, "positive_active_weeks")
        active_weeks = _optional_float(row, "active_weeks")
        if positive_weeks is not None and active_weeks is not None and active_weeks > 0:
            components["stability"] = (100.0 * _clamp(positive_weeks / active_weeks), 1.0)

    copyability = _normalize_ratio_or_score(_optional_float(row, "copy_capture_ratio"))
    if copyability is None:
        copyability = _normalize_ratio_or_score(_optional_float(row, "copyability_score"))
    if copyability is not None:
        components["copyability"] = (100.0 * copyability, 1.0)

    weighted_sum = 0.0
    effective_weight = 0.0
    full_weight = sum(PEIXAO_SCORE_V1_WEIGHTS.values())
    output_components: dict[str, float] = {}
    for name, weight in PEIXAO_SCORE_V1_WEIGHTS.items():
        if name not in components:
            output_components[f"v1_{name}"] = np.nan
            continue
        component_score, evidence_quality = components[name]
        ew = weight * _clamp(evidence_quality)
        weighted_sum += component_score * ew
        effective_weight += ew
        output_components[f"v1_{name}"] = round(component_score, 2)

    coverage = 0.0 if full_weight <= 0 else _clamp(effective_weight / full_weight)
    base_score = 0.0 if effective_weight <= 0 else weighted_sum / effective_weight
    evidence_factor = 0.65 + 0.35 * coverage
    final_score = base_score * confidence * integrity_factor * risk_factor * evidence_factor
    final_score = max(0.0, min(100.0, final_score))

    if final_score >= 90:
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

    if gate == "PASS" and coverage >= 0.85 and confidence >= 0.90 and "copyability" in components and independent_hits is not None:
        evidence_state = "DEEP_VALIDATED"
    elif gate == "PASS" and coverage >= 0.65 and confidence >= 0.80:
        evidence_state = "VALIDATED"
    else:
        evidence_state = "PROVISIONAL"

    deep_dive_candidate = bool(
        gate == "PASS"
        and integrity_factor > 0.0
        and confidence >= 0.80
        and final_score >= 60.0
    )

    return {
        "peixao_score_v1": round(final_score, 2),
        "peixao_tier_v1": tier,
        "evidence_state": evidence_state,
        "evidence_coverage": round(coverage, 4),
        "sample_confidence": round(confidence, 2),
        "winrate_wilson_lb": round(wilson_lb, 4),
        "v1_gate_status": gate,
        "v1_base_score": round(base_score, 2),
        "v1_integrity_factor": round(integrity_factor, 2),
        "v1_risk_factor": round(risk_factor, 4),
        "v1_risk_flags": ",".join(flags),
        "deep_dive_candidate": deep_dive_candidate,
        **output_components,
    }
