"""Fiação dos ciclos de 6h com provedores simulados (sem rede, sem dados V6 reais)."""
import pandas as pd

from peixao import multichain_runner as mr
from peixao import runner
from peixao.config import Settings
from tests_support import seed_cycle_inputs


def _ok(**extra):
    return lambda *a, **k: {"status": "DONE", **extra}


def _stub_legacy(monkeypatch):
    empty = pd.DataFrame()
    monkeypatch.setattr(runner, "seed_cache_if_missing", lambda *a, **k: None)
    monkeypatch.setattr(runner, "discover_offline", lambda *a, **k: {"tx_sample": empty, "actor_candidates": empty})
    monkeypatch.setattr(runner, "resolve_tx_origins", lambda *a, **k: {"origin_candidates": empty})
    monkeypatch.setattr(runner, "classify_candidates", lambda *a, **k: {"final_candidates": empty})
    monkeypatch.setattr(runner, "build_wallet_queue", lambda *a, **k: (empty, {"total": 0}))
    for name in (
        "apply_cached_gmgn", "build_alpha_v22_stage1", "record_ranked_v1_csv", "record_alpha22_stage1_csv",
        "probe_mobula", "probe_jupiter", "probe_solana_tracker", "probe_dune", "run_token_radar",
        "run_quicknode_solana_budgeted", "run_birdeye_alpha_discovery", "merge_quicknode_with_birdeye",
        "enrich_stage1_with_dune", "send_test_alert_once",
    ):
        monkeypatch.setattr(runner, name, _ok())
    monkeypatch.setattr(runner, "validate_against_v5", lambda *a, **k: [])

    def broken_probe(*a, **k):
        raise ValueError("JSON inválido com HTTP 200")

    monkeypatch.setattr(runner, "probe_gmgn_live", broken_probe)


def _cfg(tmp_path):
    project = tmp_path / "project"
    (project / "V3C_CACHE_NORMALIZED").mkdir(parents=True)
    cfg = Settings(data_dir=tmp_path / "data", project_root=project)
    cfg.ensure_dirs()
    return cfg


def test_legacy_runner_survives_provider_probe_error_and_skips_final_table(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _stub_legacy(monkeypatch)
    result = runner.run_v6(cfg=cfg)
    assert result["status"] == "DONE"
    assert result["gmgn_live"]["status"] == "ERROR"
    assert "telegram" not in result
    assert not (cfg.output_dir / "V22S_wallet_stage1.csv").exists()


def test_multichain_v6_builds_final_table_once(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    seed_cycle_inputs(cfg)
    _stub_legacy(monkeypatch)
    monkeypatch.setattr(mr, "enrich_legacy_robinhood_rotating", _ok())
    monkeypatch.setattr(mr, "enrich_coinstats_fallback", _ok())
    monkeypatch.setattr(mr, "run_base_radar", _ok(wallets=0))
    monkeypatch.setattr(mr, "run_robinhood_radar_incremental", _ok(wallets=0))
    calls = []
    real_notify = mr.notify_alpha_wallets
    monkeypatch.setattr(mr, "notify_alpha_wallets", lambda *a, **k: calls.append(a) or real_notify(*a, **k))

    result = mr.run_v6(cfg=cfg)
    assert result["final_stage"]["status"] == "DONE", result["final_stage"]
    assert len(calls) == 1
    frame = pd.read_csv(cfg.output_dir / "V22S_wallet_stage1.csv")
    assert set(frame["chain"]) == {"robinhood", "base", "solana"}
    assert frame.loc[frame["chain"].eq("solana"), "dune_new_positions_per_week"].iloc[0] == 2.5
