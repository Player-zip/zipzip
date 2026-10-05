"""Dados mínimos compartilhados pelos testes de ciclo."""
import json
import time
from pathlib import Path

import pandas as pd

from peixao.config import Settings

SOL = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWZx3T4QF3v7FfP"
EVM = "0x" + "ab" * 20


def settings_for(tmp_path: Path) -> Settings:
    cfg = Settings(data_dir=tmp_path / "data")
    cfg.ensure_dirs()
    return cfg


def seed_final_inputs(cfg: Settings) -> None:
    out = cfg.output_dir
    pd.DataFrame([{
        "address": EVM, "discovery_score": 80, "gmgn_winrate_30d": 0.66,
        "gmgn_token_num_30d": 12, "realized_profit_30d": 10000, "realized_roi_30d": 3.0,
    }]).to_csv(out / "V6_wallet_queue_enriched.csv", index=False)
    pd.DataFrame([{
        "address": EVM, "chain": "base", "nansen_evidence": True, "win_rate": 0.72,
        "closed_positions": 20, "realized_roi_30d": 0.2, "realized_roi_unit": "ratio",
    }]).to_csv(out / "V22_base_wallet_enriched.csv", index=False)
    pd.DataFrame([{
        "address": SOL, "discovery_score": 90, "win_rate": 0.71, "closed_positions": 20,
        "median_pnl_per_token": 1200, "repeatability_score": 0.75,
    }]).to_csv(out / "V22_radar_wallet_enriched.csv", index=False)
    (cfg.state_dir / "dune_selectivity_cache.json").write_text(json.dumps({"wallets": {
        SOL: {"checked_epoch": int(time.time()), "metrics": {"dune_new_positions_per_week": 2.5}},
    }}))


def seed_cycle_inputs(cfg: Settings) -> None:
    """Como em produção: a wallet Base chega pela fila de candidatas e as
    métricas pelo cache do Nansen (não por um enriched pronto)."""
    seed_final_inputs(cfg)
    (cfg.output_dir / "V22_base_wallet_enriched.csv").unlink()
    pd.DataFrame([{
        "address": EVM, "chain": "base", "independent_cross_token_hits": 3, "discovery_score": 80,
    }]).to_csv(cfg.output_dir / "V22_base_wallet_candidates.csv", index=False)
    (cfg.state_dir / "nansen_pnl_base_cache.json").write_text(json.dumps({"entries": {EVM: {
        "checked_epoch": int(time.time()),
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "metrics": {"nansen_evidence": True, "win_rate": 0.72, "closed_positions": 20,
                    "realized_roi_30d": 0.2, "realized_roi_unit": "ratio"},
    }}}))
