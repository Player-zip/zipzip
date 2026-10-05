from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

from .alpha_v22 import alpha_score_v22, wilson_lower_bound


SELECTIVE_SCORE_VERSION = "V2.2S1"
SELECTIVE_WEIGHTS = {
    "frequency": 35.0,
    "accuracy": 30.0,
    "pnl_per_position": 20.0,
    "repeatability": 15.0,
}


def _num(row, *keys: str):
    for key in keys:
        value = row.get(key)
        try:
            if value is None or pd.isna(value):
                continue
            return float(value)
        except Exception:
            continue
    return None


def _clamp(value, lo=0.0, hi=1.0):
    try:
        return max(lo, min(hi, float(value)))
    except Exception:
        return 0.0


def _sample_confidence(n: float | None) -> float:
    if n is None or n <= 0:
        return 0.0
    return _clamp(1.0 - math.exp(-float(n) / 12.0))


def _weekly_entries(row) -> tuple[float | None, float, str]:
    dune = _num(row, "dune_new_positions_per_week")
    if dune is not None:
        return max(0.0, dune), 0.90, "dune_30d_tradeflow_proxy"
    explicit = _num(row, "new_positions_per_week", "avg_new_positions_per_week")
    if explicit is not None:
        source = str(row.get("new_positions_per_week_source", "explicit") or "explicit")
        quality = 1.0 if source == "explicit" else 0.65
        return max(0.0, explicit), quality, source
    tokens30 = _num(row, "gmgn_token_num_30d")
    if tokens30 is not None:
        return max(0.0, tokens30 / (30.0 / 7.0)), 0.45, "gmgn_token_num_30d_proxy"
    return None, 0.0, "missing"


def _frequency_score(entries: float | None) -> float | None:
    if entries is None:
        return None
    x = max(0.0, float(entries))
    if x < 0.25:
        return 30.0
    if x < 1.0:
        return 30.0 + 60.0 * (x - 0.25) / 0.75
    if x <= 5.0:
        return 100.0
    if x <= 8.0:
        return 100.0 - 20.0 * (x - 5.0) / 3.0
    if x <= 12.0:
        return 80.0 - 30.0 * (x - 8.0) / 4.0
    if x <= 20.0:
        return 50.0 - 30.0 * (x - 12.0) / 8.0
    if x <= 40.0:
        return 20.0 * (1.0 - (x - 20.0) / 20.0)
    return 0.0


def _accuracy_component(row) -> tuple[float | None, float, dict]:
    wr = _num(row, "win_rate", "gmgn_winrate_30d")
    wr_source = "birdeye_or_gmgn"
    quality = 1.0
    if wr is None:
        wr = _num(row, "dune_win_rate_30d")
        wr_source = "dune_30d_tradeflow_proxy"
        quality = 0.65
    if wr is None:
        return None, 0.0, {"selective_win_rate": None, "selective_win_rate_source": "missing"}
    if wr > 1:
        wr /= 100.0
    n = _num(row, "closed_positions")
    if n is None:
        n = _num(row, "dune_closed_positions_30d")
        quality *= 0.75 if n is not None else 0.0
    confidence = _sample_confidence(n)
    wilson = wilson_lower_bound(wr, n)
    raw = _clamp((wr - 0.60) / 0.20)
    wilson_part = _clamp((wilson - 0.40) / 0.30)
    score = 100.0 * (0.55 * raw + 0.25 * wilson_part + 0.20 * confidence)
    return score, quality, {
        "selective_win_rate": round(wr, 4),
        "selective_win_rate_source": wr_source,
        "selective_sample_confidence": round(confidence, 4),
        "selective_wilson_lb": round(wilson, 4),
    }


def _pnl_per_position_component(row) -> tuple[float | None, float, float | None, str]:
    median = _num(row, "median_pnl_per_token")
    if median is not None:
        source = "median_pnl_per_token"
        quality = 1.0
    else:
        median = _num(row, "dune_median_pnl_per_position")
        if median is not None:
            source = "dune_30d_tradeflow_proxy"
            quality = 0.65
        else:
            profit = _num(row, "realized_profit_30d")
            closed = _num(row, "closed_positions")
            if profit is not None and closed is not None and closed > 0:
                median = profit / closed
                source = "realized_profit_per_closed_position_proxy"
                quality = 0.55
            else:
                return None, 0.0, None, "missing"
    score = 100.0 * _clamp(math.log1p(max(0.0, median)) / math.log1p(5_000.0))
    return score, quality, median, source


def _repeatability_component(row) -> tuple[float | None, float, str]:
    value = _num(row, "repeatability_score")
    if value is not None:
        if value > 1:
            value /= 100.0
        return 100.0 * _clamp(value), 1.0, "repeatability_score"
    value = _num(row, "dune_repeatability_score")
    if value is not None:
        return 100.0 * _clamp(value), 0.75, "dune_weekly_tradeflow_proxy"
    positive = _num(row, "positive_active_weeks", "dune_positive_active_weeks")
    active = _num(row, "active_weeks", "dune_active_weeks")
    if positive is not None and active is not None and active > 0:
        return 100.0 * _clamp(positive / active), 0.80, "positive_active_weeks"
    return None, 0.0, "missing"


def selectivity_score(row) -> dict:
    entries, frequency_quality, frequency_source = _weekly_entries(row)
    frequency = _frequency_score(entries)
    accuracy, accuracy_quality, accuracy_meta = _accuracy_component(row)
    pnl_score, pnl_quality, pnl_per_position, pnl_source = _pnl_per_position_component(row)
    repeat, repeat_quality, repeat_source = _repeatability_component(row)

    pieces = {
        "frequency": (frequency, frequency_quality),
        "accuracy": (accuracy, accuracy_quality),
        "pnl_per_position": (pnl_score, pnl_quality),
        "repeatability": (repeat, repeat_quality),
    }
    weighted = 0.0
    effective = 0.0
    full = sum(SELECTIVE_WEIGHTS.values())
    out = {}
    for name, weight in SELECTIVE_WEIGHTS.items():
        component_score, quality = pieces[name]
        output_name = "selective_pnl_per_position_score" if name == "pnl_per_position" else f"selective_{name}"
        if component_score is None or quality <= 0:
            out[output_name] = np.nan
            continue
        ew = weight * _clamp(quality)
        weighted += float(component_score) * ew
        effective += ew
        out[output_name] = round(float(component_score), 2)
    coverage = _clamp(effective / full) if full else 0.0
    score = np.nan if effective <= 0 else weighted / effective
    if entries is None:
        profile = "UNKNOWN"
    elif entries <= 5:
        profile = "SELECTIVE"
    elif entries <= 10:
        profile = "BALANCED"
    elif entries <= 20:
        profile = "ACTIVE"
    else:
        profile = "HYPERACTIVE"
    return {
        "selective_score": np.nan if pd.isna(score) else round(float(score), 2),
        "selective_evidence_coverage": round(coverage, 4),
        "selective_profile": profile,
        "selective_new_positions_per_week": None if entries is None else round(entries, 4),
        "selective_frequency_source": frequency_source,
        "selective_pnl_per_position_usd": None if pnl_per_position is None else round(float(pnl_per_position), 2),
        "selective_pnl_source": pnl_source,
        "selective_repeatability_source": repeat_source,
        **accuracy_meta,
        **out,
    }


def selective_alpha_score(row) -> dict:
    base = alpha_score_v22(row)
    sel = selectivity_score(row)
    base_score = base.get("alpha22_score")
    selectivity = sel.get("selective_score")
    sel_coverage = float(sel.get("selective_evidence_coverage", 0.0) or 0.0)
    if base_score is None or pd.isna(base_score):
        final = np.nan
    elif selectivity is not None and not pd.isna(selectivity) and sel_coverage >= 0.35:
        final = 0.65 * float(base_score) + 0.35 * float(selectivity)
    else:
        final = float(base_score) * (0.90 + 0.10 * sel_coverage)
    final = np.nan if pd.isna(final) else max(0.0, min(100.0, final))

    gate = str(base.get("alpha22_gate_status", ""))
    deep = bool(
        gate == "PASS"
        and not pd.isna(final)
        and final >= 60.0
        and float(base.get("alpha22_evidence_coverage", 0.0) or 0.0) >= 0.50
        and selectivity is not None
        and not pd.isna(selectivity)
        and float(selectivity) >= 55.0
        and sel_coverage >= 0.35
    )
    if pd.isna(final):
        tier = "UNENRICHED"
    elif final >= 90:
        tier = "S"
    elif final >= 80:
        tier = "A+"
    elif final >= 70:
        tier = "A"
    elif final >= 60:
        tier = "B"
    elif final >= 50:
        tier = "C"
    else:
        tier = "D"
    return {
        **base,
        **sel,
        "selective_score_version": SELECTIVE_SCORE_VERSION,
        "selective_alpha_score": np.nan if pd.isna(final) else round(float(final), 2),
        "selective_alpha_tier": tier,
        "selective_deep_dive_candidate": deep,
    }


def combine_wallet_inputs(legacy_path: Path, radar_path: Path | None = None) -> pd.DataFrame:
    frames = []
    for source, path in (("legacy_v6", legacy_path), ("radar_solana", radar_path)):
        if path is None or not path.is_file():
            continue
        try:
            frame = pd.read_csv(path)
        except pd.errors.EmptyDataError:
            continue
        if frame.empty or "address" not in frame.columns:
            continue
        frame = frame.copy()
        frame["selective_input_source"] = source
        frames.append(frame)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True, sort=False)
    out["address"] = out["address"].astype(str).str.strip()
    out = out[out["address"].ne("") & out["address"].ne("nan")].copy()
    out["_evidence_count"] = out.notna().sum(axis=1)
    out = out.sort_values(["address", "_evidence_count"], ascending=[True, False], kind="mergesort")
    out = out.drop_duplicates("address", keep="first").drop(columns=["_evidence_count"])
    return out.reset_index(drop=True)


def build_selective_stage1(
    legacy_enriched_path: Path,
    radar_enriched_path: Path | None,
    output_dir: Path,
    *,
    input_override: Path | None = None,
    artifact_prefix: str = "V22S",
    max_deep_dive: int = 30,
) -> dict:
    if input_override is not None and input_override.is_file():
        try:
            frame = pd.read_csv(input_override)
        except pd.errors.EmptyDataError:
            frame = pd.DataFrame()
    else:
        frame = combine_wallet_inputs(legacy_enriched_path, radar_enriched_path)
    stage1_path = output_dir / f"{artifact_prefix}_wallet_stage1.csv"
    deep_path = output_dir / f"{artifact_prefix}_deep_dive_queue.csv"
    if frame.empty:
        frame.to_csv(stage1_path, index=False)
        frame.to_csv(deep_path, index=False)
        return {"status": "DONE", "wallets": 0, "scored": 0, "selective_evidence": 0, "deep_dive_ready": 0, "stage1_output": str(stage1_path)}

    old_alpha = [c for c in frame.columns if c.startswith("alpha22_") or c.startswith("selective_")]
    if old_alpha:
        frame = frame.drop(columns=old_alpha, errors="ignore")
    scores = frame.apply(lambda row: pd.Series(selective_alpha_score(row)), axis=1)
    out = pd.concat([frame, scores], axis=1)
    discovery_sort = "discovery_score" if "discovery_score" in out.columns else "selective_evidence_coverage"
    out = out.sort_values(
        ["selective_alpha_score", "selective_evidence_coverage", discovery_sort],
        ascending=[False, False, False], na_position="last", kind="mergesort",
    ).reset_index(drop=True)
    out.to_csv(stage1_path, index=False)
    deep = out[out["selective_deep_dive_candidate"].eq(True)].head(max(0, int(max_deep_dive))).copy()
    deep.to_csv(deep_path, index=False)
    return {
        "status": "DONE",
        "score_version": SELECTIVE_SCORE_VERSION,
        "wallets": int(len(out)),
        "scored": int(out["selective_alpha_score"].notna().sum()),
        "selective_evidence": int(out["selective_score"].notna().sum()),
        "selective_profiles": {str(k): int(v) for k, v in out["selective_profile"].value_counts(dropna=False).to_dict().items()},
        "deep_dive_ready": int(len(deep)),
        "stage1_output": str(stage1_path),
        "deep_dive_output": str(deep_path),
    }
