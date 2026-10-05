from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
import numpy as np
import pandas as pd

from peixao.scoring import discovery_components, discovery_priority, peixao_score_v0, peixao_score_v1


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_wallet_queue(final_candidates: pd.DataFrame, output_dir: Path, *, top_ready: int = 100) -> tuple[pd.DataFrame, dict]:
    scored_path = output_dir / "V6_scored_wallets.csv"
    queue_path = output_dir / "V6_wallet_queue.csv"
    ready_path = output_dir / "V6_enrichment_ready.csv"
    df = final_candidates.copy()
    if df.empty:
        df.to_csv(scored_path, index=False)
        df.to_csv(queue_path, index=False)
        df.to_csv(ready_path, index=False)
        return df, {"total": 0, "eligible": 0, "ready": 0, "p0": 0, "p1": 0, "p2": 0, "p3": 0, "contracts_skipped": 0}

    for col in ("distinct_tokens", "sampled_txs"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    parts = df.apply(lambda row: pd.Series(discovery_components(row)), axis=1)
    df = pd.concat([df, parts], axis=1)
    df["priority"] = df["discovery_score"].apply(discovery_priority)
    df["eligible_enrichment"] = df["address_type"].astype(str).str.upper().eq("EOA_NO_CODE") & (df["distinct_tokens"] >= 2)

    persisted = {}
    if queue_path.is_file():
        try:
            old = pd.read_csv(queue_path)
        except Exception:
            old = pd.DataFrame()
        if not old.empty and "address" in old.columns:
            keep = [c for c in ["queue_status", "attempts", "last_provider", "last_attempt_at", "last_error", "first_queued_at"] if c in old.columns]
            for row in old[["address"] + keep].to_dict("records"):
                persisted[str(row.get("address", "")).lower()] = row

    now = _now()
    rows = []
    for row in df.to_dict("records"):
        address = str(row.get("address", "")).lower()
        prev = persisted.get(address, {})
        prev_status = str(prev.get("queue_status", ""))
        if bool(row.get("eligible_enrichment")):
            status = prev_status if prev_status in {"IN_PROGRESS", "DONE", "ERROR", "RETRY", "ENRICHED_GMGN", "READY_PROVIDER", "REVIEW_RISK"} else "READY"
        elif str(row.get("address_type", "")).upper() == "CONTRACT":
            status = "SKIP_CONTRACT"
        else:
            status = "REVIEW_ADDRESS_TYPE"
        attempts = pd.to_numeric(prev.get("attempts", 0), errors="coerce") if prev else 0
        attempts = 0 if pd.isna(attempts) else int(attempts)
        row.update({
            "queue_status": status,
            "attempts": attempts,
            "last_provider": str(prev.get("last_provider", "")) if prev else "",
            "last_attempt_at": str(prev.get("last_attempt_at", "")) if prev else "",
            "last_error": str(prev.get("last_error", "")) if prev else "",
            "first_queued_at": str(prev.get("first_queued_at", "")) if prev and str(prev.get("first_queued_at", "")) not in ("", "nan") else now,
            "updated_at": now,
        })
        rows.append(row)

    queue = pd.DataFrame(rows)
    queue["_eligible_sort"] = queue["eligible_enrichment"].astype(int)
    queue["_priority_sort"] = queue["priority"].map({"P0": 0, "P1": 1, "P2": 2, "P3": 3}).fillna(9)
    queue = queue.sort_values(
        ["_eligible_sort", "_priority_sort", "discovery_score", "distinct_tokens", "sampled_txs"],
        ascending=[False, True, False, False, False], kind="mergesort",
    ).drop(columns=["_eligible_sort", "_priority_sort"]).reset_index(drop=True)

    score_cols = ["address", "discovery_score", "priority", "eligible_enrichment", "score_recurrence", "score_source", "score_address_type", "score_activity", "source", "address_type", "distinct_tokens", "sampled_txs", "symbols"]
    queue[[c for c in score_cols if c in queue.columns]].to_csv(scored_path, index=False)
    queue.to_csv(queue_path, index=False)
    ready = queue[queue["eligible_enrichment"].eq(True) & queue["queue_status"].isin(["READY", "RETRY"])].head(int(top_ready)).copy()
    ready.to_csv(ready_path, index=False)

    summary = {
        "total": int(len(queue)), "eligible": int(queue["eligible_enrichment"].sum()), "ready": int(len(ready)),
        "p0": int((queue["priority"] == "P0").sum()), "p1": int((queue["priority"] == "P1").sum()),
        "p2": int((queue["priority"] == "P2").sum()), "p3": int((queue["priority"] == "P3").sum()),
        "contracts_skipped": int((queue["queue_status"] == "SKIP_CONTRACT").sum()),
    }
    return queue, summary


def apply_cached_gmgn(
    queue: pd.DataFrame,
    gmgn_path: Path | None,
    output_dir: Path,
    *,
    max_provider_needed: int = 100,
    max_deep_dive: int = 30,
) -> dict:
    ranked_path = output_dir / "V6_peixao_ranked.csv"
    ranked_v1_path = output_dir / "V6_peixao_ranked_v1.csv"
    deep_dive_path = output_dir / "V6_deep_dive_queue.csv"
    needed_path = output_dir / "V6_provider_needed.csv"
    enriched_queue_path = output_dir / "V6_wallet_queue_enriched.csv"
    q = queue.copy()
    q["address"] = q["address"].astype(str).str.lower()
    if not gmgn_path or not gmgn_path.is_file():
        needed = q[q["eligible_enrichment"].eq(True)].sort_values(["discovery_score", "distinct_tokens", "sampled_txs"], ascending=[False, False, False]).head(max_provider_needed)
        needed.to_csv(needed_path, index=False)
        q.head(0).to_csv(ranked_v1_path, index=False)
        q.head(0).to_csv(deep_dive_path, index=False)
        q.to_csv(enriched_queue_path, index=False)
        return {"status": "SKIPPED_NO_GMGN_CACHE", "gmgn_matched": 0, "provider_needed": int(len(needed)), "v1_scored": 0, "deep_dive_ready": 0}

    gmgn = pd.read_csv(gmgn_path).copy()
    gmgn["address"] = gmgn["address"].astype(str).str.lower()
    merged = q.merge(gmgn, on="address", how="left", suffixes=("", "_gmgn"))

    scored_v0 = merged.apply(lambda row: pd.Series(peixao_score_v0(row)), axis=1)
    scored_v1 = merged.apply(lambda row: pd.Series(peixao_score_v1(row)), axis=1)
    merged = pd.concat([merged, scored_v0, scored_v1], axis=1)

    # Keep the validated V0 artifact unchanged in meaning and ordering.
    ranked = merged[merged["peixao_score_v0"].notna()].sort_values(["peixao_score_v0", "discovery_score"], ascending=[False, False]).reset_index(drop=True)
    ranked_cols = [c for c in ["address", "peixao_score_v0", "peixao_tier", "risk_penalty", "risk_flags", "discovery_score", "priority", "source", "distinct_tokens", "sampled_txs", "gmgn_token_num_30d", "gmgn_winrate_30d", "realized_profit_30d", "realized_roi_30d", "trades_30d", "realized_profit_all", "realized_roi_all", "trades_all", "gmgn_gt_5x", "gmgn_tags", "symbols"] if c in ranked.columns]
    ranked[ranked_cols].to_csv(ranked_path, index=False)

    # V1 is a parallel score: evidence-aware and safe to evolve without breaking V0 parity.
    ranked_v1 = merged[merged["peixao_score_v1"].notna()].sort_values(
        ["peixao_score_v1", "sample_confidence", "discovery_score"],
        ascending=[False, False, False],
    ).reset_index(drop=True)
    ranked_v1_cols = [c for c in [
        "address", "peixao_score_v1", "peixao_tier_v1", "evidence_state", "evidence_coverage",
        "sample_confidence", "winrate_wilson_lb", "v1_gate_status", "v1_base_score",
        "v1_integrity_factor", "v1_risk_factor", "v1_risk_flags", "deep_dive_candidate",
        "v1_statistical_edge", "v1_independent_cross_token_edge", "v1_repeatability",
        "v1_profit_quality", "v1_profit_diversity", "v1_stability", "v1_copyability",
        "discovery_score", "priority", "source", "distinct_tokens", "sampled_txs",
        "gmgn_token_num_30d", "gmgn_winrate_30d", "realized_profit_30d", "realized_roi_30d",
        "trades_30d", "realized_profit_all", "realized_roi_all", "trades_all", "gmgn_gt_5x",
        "gmgn_tags", "symbols",
    ] if c in ranked_v1.columns]
    ranked_v1[ranked_v1_cols].to_csv(ranked_v1_path, index=False)

    deep_dive = ranked_v1[ranked_v1["deep_dive_candidate"].eq(True)].head(int(max_deep_dive)).copy()
    deep_dive[ranked_v1_cols].to_csv(deep_dive_path, index=False)

    needed = merged[merged["eligible_enrichment"].eq(True) & merged["gmgn_token_num_30d"].isna()].sort_values(["discovery_score", "distinct_tokens", "sampled_txs"], ascending=[False, False, False]).head(max_provider_needed)
    needed_cols = [c for c in ["address", "priority", "discovery_score", "source", "address_type", "distinct_tokens", "sampled_txs", "symbols", "queue_status"] if c in needed.columns]
    needed[needed_cols].to_csv(needed_path, index=False)

    merged["enrichment_source"] = np.where(merged["gmgn_token_num_30d"].notna(), "GMGN_CACHE", "")

    def status(row):
        if not bool(row.get("eligible_enrichment")):
            return row.get("queue_status", "")
        if pd.notna(row.get("peixao_score_v0")):
            risks = str(row.get("risk_flags", ""))
            return "REVIEW_RISK" if "sandwich_bot" in risks or "wash_trader" in risks else "ENRICHED_GMGN"
        return "READY_PROVIDER"

    merged["queue_status"] = merged.apply(status, axis=1)
    merged = merged.sort_values(["eligible_enrichment", "peixao_score_v0", "discovery_score"], ascending=[False, False, False], na_position="last")
    merged.to_csv(enriched_queue_path, index=False)
    return {
        "status": "DONE", "gmgn_matched": int(len(ranked)), "provider_needed": int(len(needed)),
        "a_plus": int((ranked["peixao_tier"] == "A+").sum()) if len(ranked) else 0,
        "a": int((ranked["peixao_tier"] == "A").sum()) if len(ranked) else 0,
        "v1_scored": int(len(ranked_v1)),
        "v1_s": int((ranked_v1["peixao_tier_v1"] == "S").sum()) if len(ranked_v1) else 0,
        "v1_a_plus": int((ranked_v1["peixao_tier_v1"] == "A+").sum()) if len(ranked_v1) else 0,
        "v1_a": int((ranked_v1["peixao_tier_v1"] == "A").sum()) if len(ranked_v1) else 0,
        "deep_dive_ready": int(len(deep_dive)),
    }
