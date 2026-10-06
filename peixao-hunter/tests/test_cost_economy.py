import json
import time

import pandas as pd

from peixao import config
from peixao.cost_control import budgeted_batch, record_spend, remaining_today, run_paid_step, spend_report
from peixao.priority_enrichment import enrich_nansen_pnl_priority


def test_economy_is_default_and_balanced_restores_old_volumes(monkeypatch):
    monkeypatch.delenv("PEIXAO_COST_MODE", raising=False)
    assert config.cost_mode() == "economy"
    assert config.cost_int("PEIXAO_NANSEN_WALLETS_PER_CHAIN") == 5
    monkeypatch.setenv("PEIXAO_COST_MODE", "balanced")
    assert config.cost_int("PEIXAO_NANSEN_WALLETS_PER_CHAIN") == 20
    monkeypatch.setenv("PEIXAO_NANSEN_WALLETS_PER_CHAIN", "7")
    assert config.cost_int("PEIXAO_NANSEN_WALLETS_PER_CHAIN") == 7  # ambiente vence o perfil
    monkeypatch.setenv("PEIXAO_COST_MODE", "inexistente")
    assert config.cost_mode() == "economy"


def test_daily_cap_blocks_paid_step_before_spending(tmp_path, monkeypatch):
    db = tmp_path / "m.sqlite3"
    monkeypatch.setenv("PEIXAO_DAILY_CAP_BIRDEYE", "10")
    calls = []

    def step(remaining):
        calls.append(remaining)
        return {"status": "DONE", "http_calls": 8}

    first = run_paid_step(db, "BIRDEYE", "t", step, units_from=lambda r: r["http_calls"], min_units=3)
    assert first["status"] == "DONE" and calls == [10.0]
    second = run_paid_step(db, "BIRDEYE", "t", step, units_from=lambda r: r["http_calls"], min_units=3)
    assert second["status"] == "COST_CAP_REACHED" and len(calls) == 1
    report = spend_report(db)
    assert report["BIRDEYE"]["units"] == 8 and report["BIRDEYE"]["skipped_by_cap"] == 1


def test_budgeted_batch_fits_remaining_budget(tmp_path, monkeypatch):
    db = tmp_path / "m.sqlite3"
    monkeypatch.setenv("PEIXAO_DAILY_CAP_NANSEN", "11")
    assert budgeted_batch(db, "NANSEN", 20, units_per_item=2) == 5
    record_spend(db, "NANSEN", 10)
    assert remaining_today(db, "NANSEN") == 1
    assert budgeted_batch(db, "NANSEN", 20, units_per_item=2) == 0
    monkeypatch.setenv("PEIXAO_DAILY_CAP_NANSEN", "0")
    assert budgeted_batch(db, "NANSEN", 20, units_per_item=2) == 20  # 0 = sem teto


def test_nansen_skips_low_priority_and_recent_failures(tmp_path, monkeypatch):
    from peixao import priority_enrichment as pe

    calls = []

    def fake_fetch(key, address, chain, *, timeout, lookback_days):
        calls.append(address)
        if address.endswith("bad"):
            return None, {"http_calls": 1, "statuses": [404]}
        return {"nansen_evidence": True, "win_rate": 0.7, "realized_roi_unit": "ratio"}, {"http_calls": 2, "statuses": [200, 200]}

    monkeypatch.setattr(pe, "_fetch_wallet", fake_fetch)
    monkeypatch.setattr(pe.time, "sleep", lambda s: None)
    queue = tmp_path / "q.csv"
    pd.DataFrame([
        {"address": "0xstrong", "execution_priority_score": 80},
        {"address": "0xweak", "execution_priority_score": 20},
        {"address": "0xbad", "execution_priority_score": 90},
    ]).to_csv(queue, index=False)
    kwargs = dict(api_key="k", chain="base", max_wallets=10, min_priority=45, retry_seconds=86400)
    first = enrich_nansen_pnl_priority(queue, tmp_path / "out.csv", tmp_path, **kwargs)
    assert sorted(calls) == ["0xbad", "0xstrong"]
    assert first["skipped_low_priority"] == 1
    second = enrich_nansen_pnl_priority(queue, tmp_path / "out.csv", tmp_path, **kwargs)
    assert sorted(calls) == ["0xbad", "0xstrong"]  # nada novo: cache e falha recente
    assert second["skipped_recent_failure"] == 1 and second["cache_hits"] == 1


def test_base_wallet_discovery_reuses_cache_within_ttl(tmp_path, monkeypatch):
    from peixao import base_radar

    calls = []

    def fake_discover(qualified, **kwargs):
        calls.append(1)
        pd.DataFrame([{"address": "0x" + "1" * 40}]).to_csv(kwargs["output_path"], index=False)
        return {"status": "DONE", "wallets": 1, "http_calls": 12}

    monkeypatch.setattr(base_radar, "discover_wallets_from_tokens", fake_discover)
    state = tmp_path / "state"
    state.mkdir()
    (state / "base_token_radar_v3_cache.json").write_text(json.dumps({
        "checked_epoch": int(time.time()),
        "rows": [{"token_address": "0x" + "a" * 40, "liquidity_usd": 1e6, "volume_24h_usd": 1e6, "radar_score": 90, "discovery_feed_count": 2}],
    }))
    kwargs = dict(alchemy_api_key="k", ttl_seconds=3600, wallet_ttl_seconds=3600)
    first = base_radar.run_base_radar(tmp_path / "out", state, **kwargs)
    second = base_radar.run_base_radar(tmp_path / "out", state, **kwargs)
    assert len(calls) == 1
    assert first["alchemy_calls"] == 12 and second["alchemy_calls"] == 0
    assert second["wallet_discovery_cached"] is True


def test_robinhood_prefers_free_public_rpc_in_economy(tmp_path, monkeypatch):
    from peixao import robinhood_incremental as ri

    monkeypatch.setattr(ri, "run_robinhood_radar", lambda *a, **k: {"status": "DONE", "http_calls": 0, "wallets": 0})
    paid = []
    real_radar = ri.run_robinhood_radar

    def radar(*args, **kwargs):
        if kwargs.get("alchemy_api_key"):
            paid.append(1)
        return real_radar(*args, **kwargs)

    monkeypatch.setattr(ri, "run_robinhood_radar", radar)
    monkeypatch.setattr(ri, "discover_wallets_incremental", lambda *a, **k: {"status": "DONE_NO_NEW_BLOCKS", "wallets": 0, "provider": "ROBINHOOD_PUBLIC", "rpc_calls": 2})
    out = tmp_path / "out"
    out.mkdir()
    pd.DataFrame([{"token_address": "0x" + "b" * 40}]).to_csv(out / "V22_robinhood_token_radar_shortlist.csv", index=False)
    common = dict(public_rpc_url="https://rpc.mainnet.chain.robinhood.com", alchemy_api_key="k")
    free = ri.run_robinhood_radar_incremental(out, tmp_path, prefer_free_rpc=True, **common)
    assert free["wallet_discovery_provider"] == "ROBINHOOD_PUBLIC" and not paid
    ri.run_robinhood_radar_incremental(out, tmp_path, prefer_free_rpc=False, **common)
    assert paid == [1]  # sem o perfil economy, a Alchemy vem antes


def test_chain_switch_skips_paid_steps(tmp_path, monkeypatch):
    from peixao import multichain_runner as mr
    from peixao.config import Settings

    cfg = Settings(data_dir=tmp_path / "data", chains=("solana",))
    assert mr._base_discovery(cfg, ttl_seconds=60)["status"] == "CHAIN_DISABLED"
    assert mr._robinhood_discovery(cfg, ttl_seconds=60)["status"] == "CHAIN_DISABLED"


def test_quicknode_skipped_in_fast_cycle_under_economy(tmp_path, monkeypatch):
    import peixao.stream_discovery as sd
    from peixao.config import Settings

    cfg = Settings(data_dir=tmp_path / "data")
    done = lambda *a, **k: {"status": "DONE"}  # noqa: E731
    calls = []
    monkeypatch.setattr(sd, "_stream_first_robinhood", done)
    monkeypatch.setattr(sd, "_solana_token_radar", done)
    monkeypatch.setattr(sd, "_base_discovery", done)
    monkeypatch.setattr(sd, "build_execution_queue", done)
    monkeypatch.setattr(sd, "_quicknode_discovery", lambda cfg: calls.append(1) or {"status": "DONE"})

    monkeypatch.delenv("PEIXAO_QUICKNODE_FAST_CYCLE", raising=False)
    monkeypatch.delenv("PEIXAO_COST_MODE", raising=False)
    result = sd.run_discovery_cycle_stream_first(cfg=cfg)
    assert result["solana_wallets"]["status"] == "SKIPPED_COST_PROFILE" and not calls

    monkeypatch.setenv("PEIXAO_QUICKNODE_FAST_CYCLE", "1")
    sd.run_discovery_cycle_stream_first(cfg=cfg)
    assert calls == [1]
