from __future__ import annotations

from pathlib import Path

import pandas as pd

from .evidence_ledger import (
    CACHE_SYNCED_PROVIDERS,
    DEFAULT_EVIDENCE_MAX_AGE_DAYS,
    DEFAULT_EVIDENCE_RETENTION_DAYS,
    ROW_METHODOLOGY,
    _connect,
    canonical_metrics_for_wallet,
    normalize_address,
    normalize_chain,
    prune_evidence,
    record_metrics,
    wallet_key,
)
from .units import ROI_UNIT_KEY


PERFORMANCE_FIELDS = (
    "win_rate", "gmgn_winrate_30d", "realized_profit_30d", "realized_roi_30d",
    "closed_positions", "total_trades", "repeatability_score", "median_pnl_per_token",
    "profit_hhi", "largest_win_share", "top3_profit_share", "new_positions_per_week",
    "winning_tokens", "tokens_traded", "temporal_consistency_score",
    "positive_active_weeks", "active_weeks",
)


def _read(path: Path | None) -> pd.DataFrame:
    try:
        return pd.read_csv(path) if path is not None and path.is_file() else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def _present(value) -> bool:
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except Exception:
        pass
    return str(value).strip().lower() not in {"", "nan", "none", "null"}


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return _present(value) and str(value).strip().lower() in {"1", "true", "yes", "y"}


def _provider(row: dict) -> str:
    if _truthy(row.get("nansen_evidence")):
        return "NANSEN"
    if _truthy(row.get("zerion_evidence")):
        return "ZERION"
    if _truthy(row.get("coinstats_evidence")):
        return "COINSTATS"
    if any(str(k).startswith("birdeye_") and _present(v) for k, v in row.items()):
        return "BIRDEYE"
    if _present(row.get("gmgn_winrate_30d")) or _present(row.get("gmgn_token_num_30d")):
        return "GMGN_CACHE"
    return "INLINE"


def _row_provider(row: dict) -> str:
    """Provedor usado para gravar a linha do pipeline no ledger.

    Uma linha pode misturar campos do GMGN com lacunas preenchidas por
    Nansen/Zerion/CoinStats/Birdeye. Esses provedores já entram no ledger pelos
    próprios caches (com data e atribuição corretas), então a linha mista entra
    como INLINE, a menor prioridade, em vez de herdar a prioridade do provedor.
    """
    provider = _provider(row)
    return "INLINE" if provider in CACHE_SYNCED_PROVIDERS else provider


def build_chain_aware_inputs(
    *,
    legacy_path: Path | None,
    solana_path: Path | None,
    base_path: Path | None,
    robinhood_path: Path | None,
    output_path: Path,
    db_path: Path,
    dune_metrics: dict[str, dict] | None = None,
    evidence_max_age_days: float | None = DEFAULT_EVIDENCE_MAX_AGE_DAYS,
    evidence_retention_days: float | None = DEFAULT_EVIDENCE_RETENTION_DAYS,
) -> dict:
    specs = (
        ("legacy_v6", legacy_path, "robinhood"),
        ("radar_solana", solana_path, "solana"),
        ("radar_base", base_path, "base"),
        ("radar_robinhood", robinhood_path, "robinhood"),
    )
    frames: list[pd.DataFrame] = []
    for source, path, default_chain in specs:
        frame = _read(path)
        if frame.empty or "address" not in frame.columns:
            continue
        frame = frame.copy()
        if "chain" not in frame.columns:
            frame["chain"] = default_chain
        raw = frame["chain"].fillna("").astype(str).str.strip().str.lower()
        frame["chain"] = raw.mask(raw.isin(["", "nan", "none"]), default_chain)
        frame["selective_input_source"] = source
        frames.append(frame)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not frames:
        pd.DataFrame().to_csv(output_path, index=False)
        return {"status": "DONE_EMPTY", "wallets": 0, "by_chain": {}, "output": str(output_path)}

    out = pd.concat(frames, ignore_index=True, sort=False)
    out["chain"] = out["chain"].map(normalize_chain)
    out["address"] = [normalize_address(c, a) for c, a in zip(out["chain"], out["address"], strict=True)]
    out = out[
        out["address"].astype(str).str.strip().ne("")
        & out["address"].astype(str).str.lower().ne("nan")
    ].copy()
    out["wallet_key"] = [wallet_key(c, a) for c, a in zip(out["chain"], out["address"], strict=True)]
    out["_evidence_count"] = out.notna().sum(axis=1)
    out = out.sort_values(["wallet_key", "_evidence_count"], ascending=[True, False], kind="mergesort")
    out = out.drop_duplicates("wallet_key", keep="first").drop(columns=["_evidence_count"]).reset_index(drop=True)

    dune = dune_metrics or {}
    rows: list[dict] = []
    normalization_issues = 0
    dune_applied = 0
    conn = _connect(db_path)
    try:
        for row in out.to_dict("records"):
            chain = str(row.get("chain"))
            address = str(row.get("address"))
            metrics = {field: row.get(field) for field in PERFORMANCE_FIELDS if field in row}
            if _present(row.get(ROI_UNIT_KEY)):
                metrics[ROI_UNIT_KEY] = row.get(ROI_UNIT_KEY)
            for flag in ("zerion_roi_unit",):
                if _present(row.get(flag)):
                    metrics[flag] = row.get(flag)
            recorded = record_metrics(
                db_path,
                chain=chain,
                address=address,
                provider=_row_provider(row),
                metrics=metrics,
                observed_at=str(row.get("observed_at") or row.get("updated_at") or "") or None,
                window_days=30,
                methodology=ROW_METHODOLOGY,
                source_quality=0.75,
                conn=conn,
            )
            normalization_issues += len(recorded.get("issues", []))
            canonical = canonical_metrics_for_wallet(
                db_path, chain, address, conn=conn, max_age_days=evidence_max_age_days,
            )
            # O ledger é a fonte da verdade: valor que ele rejeitou (unidade
            # inválida/ambígua) não pode voltar pela linha bruta.
            for field in PERFORMANCE_FIELDS:
                row[field] = canonical.get(field)
            row[ROI_UNIT_KEY] = "ratio"
            for field in (
                "wallet_key", "evidence_sources", "evidence_source_count", "evidence_confidence",
                "evidence_disagreement_wr", "evidence_disagreement_roi", "evidence_stale_metrics",
                "metric_normalization_issues",
            ):
                row[field] = canonical.get(field)
            if chain == "solana" and isinstance(dune.get(address), dict):
                row.update(dune[address])
                dune_applied += 1
            rows.append(row)
        pruned = prune_evidence(db_path, retention_days=evidence_retention_days or 0, conn=conn)
        conn.commit()
    finally:
        conn.close()

    result = pd.DataFrame(rows)
    result.to_csv(output_path, index=False)
    by_chain = {str(k): int(v) for k, v in result["chain"].value_counts().to_dict().items()}
    return {
        "status": "DONE", "wallets": int(len(result)), "by_chain": by_chain,
        "normalization_issues": int(normalization_issues), "dune_applied": int(dune_applied),
        "evidence_pruned": int(pruned.get("deleted", 0)), "output": str(output_path),
    }
