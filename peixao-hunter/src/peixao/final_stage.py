"""Construtor único da tabela final ``V22S_wallet_stage1.csv``.

É a tabela lida por ``/status``, ``/wallets_bs``, alertas, backtest e fila
adaptativa. Os ciclos de 1h e de 6h chamam esta função; nenhum outro código
escreve o arquivo. A identidade da wallet é ``chain:address``, as métricas vêm
do ledger normalizado e a validação Dune (ciclo de 6h) entra pelo cache.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .chain_aware_inputs import build_chain_aware_inputs
from .config import Settings
from .dune_selectivity import load_dune_metrics
from .evidence_ledger import sync_provider_caches
from .selective_alpha import build_selective_stage1
from .selective_db import record_selective_stage1_csv
from .state import atomic_csv, safe_call

FINAL_STAGE1 = "V22S_wallet_stage1.csv"


def first_existing(*paths: Path | None) -> Path | None:
    for path in paths:
        if path is not None and path.is_file():
            return path
    return paths[-1] if paths else None


def _read_csv(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path) if path.is_file() else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def _evm_chain_input(out: Path, chain: str) -> Path:
    """Fila completa da rede + linhas enriquecidas desta rodada.

    O arquivo ``V22_{chain}_wallet_enriched.csv`` só traz as wallets vencidas
    na fila adaptativa. Sem a fila completa, wallets "em monitoramento" sumiam
    da tabela final por uma hora e voltavam na seguinte. As métricas das que
    não foram consultadas agora vêm do ledger (caches dos provedores).
    """
    enriched = _read_csv(out / f"V22_{chain}_wallet_enriched.csv")
    queue = _read_csv(out / "V22_execution_queue.csv")
    if not queue.empty and "chain" in queue.columns:
        queue = queue[queue["chain"].astype(str).str.strip().str.lower().eq(chain)]
    frames = [f for f in (enriched, queue) if not f.empty and "address" in f.columns]
    combined = pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()
    path = out / f"V23_{chain}_final_inputs.csv"
    atomic_csv(combined, path)
    return path


def build_final_stage1(cfg: Settings) -> dict:
    cfg.ensure_dirs()
    out = cfg.output_dir
    db_path = cfg.master_db

    cache_sync = safe_call("evidence_cache_sync", lambda: sync_provider_caches(cfg.state_dir, db_path))

    legacy_input = first_existing(
        out / "V22_legacy_robinhood_wallet_enriched.csv",
        out / "V6_wallet_queue_enriched.csv",
    )
    if legacy_input is None or not legacy_input.is_file():
        return {"status": "NO_BASELINE", "cache_sync": cache_sync}

    solana_input = first_existing(
        out / "V22_radar_wallet_enriched_quicknode.csv",
        out / "V22_quicknode_wallet_candidates.csv",
        out / "V22_radar_wallet_enriched.csv",
    )
    dune = safe_call("dune_cache", lambda: load_dune_metrics(
        cfg.state_dir, max_age_seconds=cfg.dune_selectivity_max_age_seconds,
    ))
    dune_metrics = dune if isinstance(dune, dict) and dune.get("status") != "ERROR" else {}

    canonical_path = out / "V23_canonical_wallet_inputs.csv"
    canonical = safe_call("chain_aware_canonical_inputs", lambda: build_chain_aware_inputs(
        legacy_path=legacy_input,
        solana_path=solana_input,
        base_path=_evm_chain_input(out, "base"),
        robinhood_path=_evm_chain_input(out, "robinhood"),
        output_path=canonical_path,
        db_path=db_path,
        dune_metrics=dune_metrics,
        evidence_max_age_days=cfg.evidence_max_age_days,
        evidence_retention_days=cfg.evidence_retention_days,
    ))
    if canonical.get("status") == "ERROR":
        # Não publica uma tabela final montada a partir de insumos velhos.
        return {"status": "ERROR", "cache_sync": cache_sync, "canonical_inputs": canonical}

    selective = safe_call("selective_alpha", lambda: build_selective_stage1(
        legacy_input,
        None,
        out,
        input_override=canonical_path,
        artifact_prefix="V22S",
        max_deep_dive=30,
    ))
    selective_db = safe_call("selective_db", lambda: record_selective_stage1_csv(out / FINAL_STAGE1, db_path))

    parts = (canonical, selective, selective_db)
    return {
        "status": "DONE" if all(x.get("status") != "ERROR" for x in parts) else "PARTIAL",
        "cache_sync": cache_sync,
        "canonical_inputs": canonical,
        "dune_wallets": len(dune_metrics),
        "selective_alpha": selective,
        "selective_db": selective_db,
        "output": str(out / FINAL_STAGE1),
    }
