import json
import time

import pandas as pd
import pytest

from peixao import bot_jobs
from peixao import priority_enrichment as pe
from peixao import telegram_auth as ta
from peixao.backlog import classify_waiting, diagnosis_text
from peixao.config import Settings
from peixao.priority_validation import run_priority_validation_cycle
from test_telegram_security import FakeTelegram
from tests_support import EVM, seed_cycle_inputs

ADMIN = "900"
NEW_1 = "0x" + "1" * 40
NEW_2 = "0x" + "2" * 40
NO_DATA = "0x" + "3" * 40
CONTRACT = "0x" + "c" * 40


def _cfg(tmp_path, **keys) -> Settings:
    cfg = Settings(data_dir=tmp_path / "data", **keys)
    cfg.ensure_dirs()
    return cfg


def _seed_backlog(cfg):
    seed_cycle_inputs(cfg)
    out = cfg.output_dir
    legacy = pd.read_csv(out / "V6_wallet_queue_enriched.csv")
    legacy = pd.concat([legacy, pd.DataFrame([{
        "address": CONTRACT, "address_type": "CONTRACT", "eligible_enrichment": False, "queue_status": "SKIP_CONTRACT",
    }])], ignore_index=True)
    legacy.to_csv(out / "V6_wallet_queue_enriched.csv", index=False)
    pd.DataFrame([
        {"address": EVM, "chain": "base", "independent_cross_token_hits": 3, "discovery_score": 80},
        {"address": NEW_1, "chain": "base", "independent_cross_token_hits": 3, "discovery_score": 90},
        {"address": NEW_2, "chain": "base", "independent_cross_token_hits": 2, "discovery_score": 60},
        {"address": NO_DATA, "chain": "base", "independent_cross_token_hits": 2, "discovery_score": 50},
    ]).to_csv(out / "V22_base_wallet_candidates.csv", index=False)
    cache = json.loads((cfg.state_dir / "nansen_pnl_base_cache.json").read_text())
    cache["entries"][NO_DATA] = {"checked_epoch": int(time.time()), "checked_at": "x", "metrics": {"nansen_evidence": True, "win_rate": None}}
    (cfg.state_dir / "nansen_pnl_base_cache.json").write_text(json.dumps(cache))
    run_priority_validation_cycle(cfg=cfg)


def test_diagnosis_classifies_waiting_wallets(tmp_path):
    cfg = _cfg(tmp_path)
    _seed_backlog(cfg)
    waiting = classify_waiting(cfg)
    reasons = dict(zip(waiting["wallet_key"], waiting["reason"], strict=True))
    assert reasons[f"base:{NEW_1}"] == "nunca_consultada"
    assert reasons[f"base:{NO_DATA}"] == "sem_historico"
    assert reasons[f"robinhood:{CONTRACT}"] == "nao_elegivel"
    text = diagnosis_text(cfg)
    assert "🔵 BASE" in text and "nunca consultadas" in text and "/acelerar base" in text
    # /status separa os não elegíveis do "aguardando"
    status = ta._base_status_text(cfg.data_dir)
    assert "🚫 Não elegíveis (contrato/tipo): 1" in status


@pytest.fixture()
def tg(monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setattr(ta, "_api", fake.api)
    return fake


def test_acelerar_flow_with_confirmation_and_zerion_fallback(tmp_path, tg, monkeypatch):
    _seed_backlog(_cfg(tmp_path))  # seed sem chaves: nenhuma chamada real
    cfg = _cfg(tmp_path, nansen_api_key="n", zerion_api_key="z")
    monkeypatch.setattr(ta, "_cfg_for", lambda db_path: cfg)

    nansen_calls, zerion_calls = [], []

    def fake_nansen(key, address, chain, *, timeout, lookback_days):
        nansen_calls.append(address)
        return None, {"http_calls": 1, "statuses": [403]}  # Nansen bloqueado

    def fake_zerion(key, address, chain, *, timeout, lookback_days):
        zerion_calls.append(address)
        return {"zerion_evidence": True, "win_rate": 0.7, "closed_positions": 15, "realized_roi_unit": "ratio",
                "zerion_roi_unit": "ratio", "realized_roi_30d": 0.2}, {"http_calls": 1, "statuses": [200]}

    monkeypatch.setattr(pe, "_fetch_wallet", fake_nansen)
    monkeypatch.setattr("peixao.zerion_evm.fetch_zerion_wallet_pnl", fake_zerion)
    monkeypatch.setattr(pe.time, "sleep", lambda s: None)

    def run(*texts):
        for text in texts:
            tg.say(text, chat=ADMIN)
        ta.process_auth_updates(token="T", access_password="segredo", db_path=cfg.master_db, admin_chat_ids=(ADMIN,))

    run("/gargalo")
    assert "GARGALO" in tg.sent()[-1]
    run("/acelerar base 2")
    estimate = tg.sent()[-1]
    assert "ACELERAR BASE" in estimate and "/confirmar" in estimate
    code = estimate.split("/confirmar ")[1].split()[0]

    # Sem confirmação nada roda
    assert bot_jobs.run_next_job(cfg) is None
    run(f"/confirmar {code}")
    assert "na fila" in tg.sent()[-1]

    messages = []
    result = bot_jobs.run_next_job(cfg, notify=lambda chat, text: messages.append((chat, text)))
    assert result["status"] == "DONE"
    assert sorted(zerion_calls) == sorted([NEW_1, NEW_2])  # Nansen falhou -> Zerion
    assert messages and messages[0][0] == ADMIN
    assert "ganharam win rate: 2" in messages[0][1]
    frame = pd.read_csv(cfg.output_dir / "V22S_wallet_stage1.csv")
    assert frame.loc[frame["wallet_key"].eq(f"base:{NEW_1}"), "win_rate"].iloc[0] == 0.7


def test_checar_and_non_admin_refused(tmp_path, tg, monkeypatch):
    _seed_backlog(_cfg(tmp_path))
    cfg = _cfg(tmp_path, nansen_api_key="n")
    monkeypatch.setattr(ta, "_cfg_for", lambda db_path: cfg)
    monkeypatch.setattr(pe, "_fetch_wallet", lambda *a, **k: (
        {"nansen_evidence": True, "win_rate": 0.81, "closed_positions": 30, "realized_roi_unit": "ratio"},
        {"http_calls": 2, "statuses": [200, 200]},
    ))
    monkeypatch.setattr(pe.time, "sleep", lambda s: None)

    tg.say("/checar " + NO_DATA, chat="555")
    tg.say("/checar " + NO_DATA + " base", chat=ADMIN)
    ta.process_auth_updates(token="T", access_password="segredo", db_path=cfg.master_db, admin_chat_ids=(ADMIN,))
    assert any("Acesso bloqueado" in t for t in tg.sent())
    assert "na fila" in tg.sent()[-1]

    messages = []
    bot_jobs.run_next_job(cfg, notify=lambda chat, text: messages.append(text))
    assert "CHECAGEM BASE" in messages[0] and "Win rate: 81.0%" in messages[0]


def test_no_data_wallets_are_not_requeried_until_window(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(pe, "_fetch_wallet", lambda key, address, chain, **k: (
        calls.append(address) or ({"nansen_evidence": True, "win_rate": None}, {"http_calls": 2, "statuses": [200, 200]})
    ))
    monkeypatch.setattr(pe.time, "sleep", lambda s: None)
    queue = tmp_path / "q.csv"
    pd.DataFrame([{"address": NEW_1}]).to_csv(queue, index=False)
    kwargs = dict(api_key="k", chain="base", max_wallets=5, ttl_seconds=0, no_data_ttl_seconds=14 * 86400)
    pe.enrich_nansen_pnl_priority(queue, tmp_path / "o.csv", tmp_path, **kwargs)
    pe.enrich_nansen_pnl_priority(queue, tmp_path / "o.csv", tmp_path, **kwargs)
    assert calls == [NEW_1]


def test_boost_respects_daily_cap_unless_extra(tmp_path, monkeypatch):
    _seed_backlog(_cfg(tmp_path))
    cfg = _cfg(tmp_path, nansen_api_key="n")
    monkeypatch.setenv("PEIXAO_DAILY_CAP_NANSEN", "2")
    capped = bot_jobs.estimate_boost(cfg, "base", 2, extra=False)
    nansen = next(p for p in capped["providers"] if p["provider"] == "NANSEN")
    assert nansen["wallets"] == 1  # 2 unidades = 1 wallet
    assert "extra" in bot_jobs.format_estimate(capped, "1234")
    extra = bot_jobs.estimate_boost(cfg, "base", 2, extra=True)
    assert next(p for p in extra["providers"] if p["provider"] == "NANSEN")["wallets"] == 2
