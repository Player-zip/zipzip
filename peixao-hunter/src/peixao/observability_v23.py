from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import os

import pandas as pd

from .evidence_ledger import provider_health, record_provider_run


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path) if path.is_file() else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def _num(value, default=0):
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def record_priority_provider_stats(db_path: Path, *, base_nansen: dict, robinhood_nansen: dict) -> dict:
    recorded = 0
    for chain, item in (("base", base_nansen), ("robinhood", robinhood_nansen)):
        if not isinstance(item, dict):
            continue
        record_provider_run(
            db_path,
            provider="NANSEN",
            chain=chain,
            attempted=int(item.get("live_attempted", item.get("attempted", 0)) or 0),
            enriched=int(item.get("live_enriched", 0) or 0),
            errors=int(item.get("errors", 0) or 0),
            http_calls=int(item.get("http_calls", 0) or 0),
            credits=float(item.get("credits_used", 0.0) or 0.0),
            statuses=list(item.get("http_statuses", []) or []),
        )
        recorded += 1
    return {"status": "DONE", "provider_runs_recorded": recorded}


def _waiting_reasons(stage: pd.DataFrame) -> dict:
    reasons = {"no_winrate": 0, "low_sample": 0, "missing_pnl": 0, "missing_repeatability": 0, "other": 0}
    if stage.empty:
        return reasons
    score = pd.to_numeric(stage.get("selective_alpha_score", pd.Series(index=stage.index, dtype=float)), errors="coerce")
    waiting = stage.loc[score.isna()].copy()
    for row in waiting.to_dict("records"):
        wr = _num(row.get("win_rate"), None)
        if wr is None:
            wr = _num(row.get("gmgn_winrate_30d"), None)
        sample = _num(row.get("closed_positions"), None)
        pnl = _num(row.get("realized_profit_30d"), None)
        repeat = _num(row.get("repeatability_score"), None)
        if wr is None:
            reasons["no_winrate"] += 1
        elif sample is None or sample < 10:
            reasons["low_sample"] += 1
        elif pnl is None:
            reasons["missing_pnl"] += 1
        elif repeat is None:
            reasons["missing_repeatability"] += 1
        else:
            reasons["other"] += 1
    return reasons


def publish_efficiency_snapshot(
    *,
    output_dir: Path,
    state_dir: Path,
    db_path: Path,
    adaptive_queue: dict | None = None,
    backtest: dict | None = None,
) -> dict:
    stage = _read(output_dir / "V22S_wallet_stage1.csv")
    if stage.empty:
        stage = _read(output_dir / "V22S_wallet_stage1_completed.csv")
    total = int(len(stage))
    scores = pd.to_numeric(stage.get("selective_alpha_score", pd.Series(index=stage.index, dtype=float)), errors="coerce")
    scored = int(scores.notna().sum())
    gates = stage.get("alpha22_gate_status", pd.Series(index=stage.index, dtype=str)).astype(str).str.upper() if not stage.empty else pd.Series(dtype=str)
    if "selective_deep_dive_candidate" in stage.columns:
        deep = stage["selective_deep_dive_candidate"].astype(str).str.lower().isin(["true", "1", "yes"])
        alpha = int((scores.ge(70) & deep).sum())
    else:
        alpha = int((scores.ge(70) & gates.eq("PASS")).sum()) if not scores.empty else 0

    health = {}
    for provider in ("NANSEN", "ZERION", "COINSTATS"):
        item = provider_health(db_path, provider, hours=24)
        enriched = int(item.get("enriched", 0) or 0)
        credits = float(item.get("credits", 0.0) or 0.0)
        calls = int(item.get("http_calls", 0) or 0)
        item["credits_per_enriched"] = None if not enriched else round(credits / enriched, 4)
        item["calls_per_enriched"] = None if not enriched else round(calls / enriched, 3)
        health[provider.lower()] = item

    backtest_frame = _read(output_dir / "V22_backtest_summary.csv")
    payload = {
        "version": "V2.3-DELTA",
        "updated_at": _now(),
        "funnel": {
            "wallets": total,
            "scored": scored,
            "waiting": max(0, total - scored),
            "pass": int(gates.eq("PASS").sum()) if not gates.empty else 0,
            "low_sample": int(gates.eq("PASS_LOW_SAMPLE").sum()) if not gates.empty else 0,
            "reject_wr": int(gates.eq("REJECT_WR").sum()) if not gates.empty else 0,
            "alpha": alpha,
        },
        "waiting_reasons": _waiting_reasons(stage),
        "providers_24h": health,
        "adaptive_queue": adaptive_queue or {},
        "backtest": backtest or {},
        "backtest_summary_rows": backtest_frame.to_dict("records")[:12] if not backtest_frame.empty else [],
    }
    path = state_dir / "enrichment_efficiency.json"
    _atomic_json(path, payload)
    return {"status": "DONE", "output": str(path), **payload["funnel"]}
