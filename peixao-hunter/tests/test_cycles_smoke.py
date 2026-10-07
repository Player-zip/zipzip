import importlib.util
from pathlib import Path

import pandas as pd

from peixao.priority_validation import run_priority_validation_cycle
from tests_support import EVM, seed_cycle_inputs, settings_for


def test_hourly_cycle_builds_final_table_and_keeps_monitored_wallets(tmp_path):
    cfg = settings_for(tmp_path)
    seed_cycle_inputs(cfg)
    first = run_priority_validation_cycle(cfg=cfg)
    assert first["final_stage"]["status"] == "DONE", first["final_stage"]
    assert first["telegram"]["status"] == "NOT_CONFIGURED"
    frame = pd.read_csv(cfg.output_dir / "V22S_wallet_stage1.csv")
    assert set(frame["chain"]) == {"robinhood", "base", "solana"}
    base = frame[frame["chain"].eq("base")].iloc[0]
    assert base["win_rate"] == 0.72  # veio do cache do Nansen via ledger

    # Na rodada seguinte a wallet já tem score e fica "em monitoramento" (fora
    # da lista vencida); ela não pode sumir da tabela final.
    second = run_priority_validation_cycle(cfg=cfg)
    assert second["queue"]["monitor_suppressed"] >= 1
    frame = pd.read_csv(cfg.output_dir / "V22S_wallet_stage1.csv")
    assert f"base:{EVM}" in set(frame["wallet_key"])


def test_worker_daemon_imports():
    path = Path(__file__).resolve().parents[1] / "scripts" / "worker_daemon.py"
    spec = importlib.util.spec_from_file_location("worker_daemon_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.stream_flush_loop)
