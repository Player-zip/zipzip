import json
import re
import sqlite3
import time
from pathlib import Path

import pandas as pd

from peixao.config import Settings
from peixao.final_stage import build_final_stage1

SOL = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWZx3T4QF3v7FfP"
EVM = "0x" + "ab" * 20


def _cfg(tmp_path: Path) -> Settings:
    cfg = Settings(data_dir=tmp_path / "data")
    cfg.ensure_dirs()
    return cfg


def _seed(cfg: Settings) -> None:
    out = cfg.output_dir
    # V6 legado (Robinhood) sem coluna chain, endereço em checksum/maiúsculas.
    pd.DataFrame([{
        "address": EVM.upper().replace("0X", "0x"), "discovery_score": 80, "gmgn_winrate_30d": 0.66,
        "gmgn_token_num_30d": 12, "realized_profit_30d": 10000, "realized_roi_30d": 3.0,
    }]).to_csv(out / "V6_wallet_queue_enriched.csv", index=False)
    # Mesmo endereço EVM na Base, enriquecido pelo Nansen (ROI já em razão).
    pd.DataFrame([{
        "address": EVM, "chain": "base", "nansen_evidence": True, "win_rate": 0.72,
        "closed_positions": 20, "realized_roi_30d": 0.2, "realized_roi_unit": "ratio",
    }]).to_csv(out / "V22_base_wallet_enriched.csv", index=False)
    pd.DataFrame([{
        "address": SOL, "discovery_score": 90, "win_rate": 0.71, "closed_positions": 20,
        "median_pnl_per_token": 1200, "repeatability_score": 0.75, "weighted_cross_token_score": 0.8,
    }]).to_csv(out / "V22_radar_wallet_enriched.csv", index=False)
    (cfg.state_dir / "dune_selectivity_cache.json").write_text(json.dumps({"wallets": {
        SOL: {"checked_epoch": int(time.time()), "metrics": {"wallet": SOL, "dune_new_positions_per_week": 2.5, "dune_win_rate_30d": 0.6}},
    }}))


def test_final_table_is_chain_aware_and_keeps_dune(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    result = build_final_stage1(cfg)
    assert result["status"] == "DONE", result
    frame = pd.read_csv(cfg.output_dir / "V22S_wallet_stage1.csv")
    assert sorted(frame["wallet_key"]) == sorted([f"robinhood:{EVM}", f"base:{EVM}", f"solana:{SOL}"])
    solana = frame[frame["chain"].eq("solana")].iloc[0]
    assert solana["dune_new_positions_per_week"] == 2.5
    legacy = frame[frame["chain"].eq("robinhood")].iloc[0]
    assert legacy["realized_roi_30d"] == 3.0  # GMGN já é razão: não vira 0.03
    assert set(frame["realized_roi_unit"]) == {"ratio"}
    assert result["dune_wallets"] == 1


def test_rebuilding_final_table_does_not_grow_the_ledger(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    build_final_stage1(cfg)
    with sqlite3.connect(cfg.master_db) as conn:
        first = conn.execute("SELECT COUNT(*) FROM wallet_evidence").fetchone()[0]
    build_final_stage1(cfg)
    build_final_stage1(cfg)
    with sqlite3.connect(cfg.master_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM wallet_evidence").fetchone()[0] == first


def test_no_baseline_does_not_touch_final_table(tmp_path):
    cfg = _cfg(tmp_path)
    assert build_final_stage1(cfg)["status"] == "NO_BASELINE"
    assert not (cfg.output_dir / "V22S_wallet_stage1.csv").exists()


def test_only_final_stage_builds_the_published_table():
    src = Path(__file__).resolve().parents[1] / "src" / "peixao"
    writers = []
    for path in src.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r'artifact_prefix="V22S"', text):
            writers.append(path.name)
    assert writers == ["final_stage.py"]
