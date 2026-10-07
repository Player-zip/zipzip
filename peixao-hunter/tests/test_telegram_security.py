import sqlite3
import time

import pandas as pd
import pytest

from peixao import telegram_auth as ta
from peixao import telegram_notifier as tn

CHAT = "111"


class FakeTelegram:
    def __init__(self):
        self.updates = []
        self.calls = []
        self.next_id = 1

    def say(self, text, chat=CHAT):
        self.updates.append({
            "update_id": self.next_id,
            "message": {"message_id": 1000 + self.next_id, "chat": {"id": int(chat)}, "text": text},
        })
        self.next_id += 1

    def api(self, token, method, params=None, timeout=10.0):
        params = dict(params or {})
        self.calls.append((method, params))
        if method == "getUpdates":
            offset = int(params.get("offset", 0))
            return {"ok": True, "result": [u for u in self.updates if u["update_id"] >= offset]}
        return {"ok": True}

    def sent(self):
        return [p["text"] for m, p in self.calls if m == "sendMessage"]

    def deleted(self):
        return [p["message_id"] for m, p in self.calls if m == "deleteMessage"]


@pytest.fixture()
def tg(monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setattr(ta, "_api", fake.api)
    return fake


def _run(db, password="segredo", **kwargs):
    return ta.process_auth_updates(token="T", access_password=password, db_path=db, **kwargs)


def test_lockout_after_failed_attempts_and_password_is_deleted(tmp_path, tg):
    db = tmp_path / "m.sqlite3"
    tg.say("/start")
    for _ in range(3):
        tg.say("errada")
    tg.say("segredo")  # bloqueado: nem é avaliada
    _run(db, max_attempts=3, lockout_seconds=600)
    with sqlite3.connect(db) as conn:
        authorized, locked_until = conn.execute(
            "SELECT authorized, locked_until FROM telegram_auth WHERE chat_id=?", (CHAT,)
        ).fetchone()
    assert authorized == 0
    assert locked_until > time.time()
    assert "Muitas tentativas" in tg.sent()[-1]
    assert len(tg.deleted()) == 4


def test_password_rotation_revokes_sessions(tmp_path, tg):
    db = tmp_path / "m.sqlite3"
    tg.say("/start")
    tg.say("segredo")
    result = _run(db)
    assert result["authorized"] == 1
    with sqlite3.connect(db) as conn:
        assert ta.authorized_chat_ids(conn, "segredo") == [CHAT]
        assert ta.authorized_chat_ids(conn, "nova-senha") == []

    tg.say("/status")
    _run(db, password="nova-senha")
    assert "Acesso bloqueado" in tg.sent()[-1]


def test_logout_and_ttl(tmp_path, tg):
    db = tmp_path / "m.sqlite3"
    tg.say("/start")
    tg.say("segredo")
    _run(db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE telegram_auth SET authorized_epoch=? WHERE chat_id=?", (int(time.time()) - 10 * 86400, CHAT))
        conn.commit()
        assert ta.authorized_chat_ids(conn, "segredo", ttl_days=7) == []
        assert ta.authorized_chat_ids(conn, "segredo", ttl_days=0) == [CHAT]
    tg.say("/logout")
    _run(db)
    with sqlite3.connect(db) as conn:
        assert ta.authorized_chat_ids(conn, "segredo") == []


def test_legacy_sessions_are_bound_to_current_password(tmp_path, tg):
    db = tmp_path / "m.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE telegram_auth (chat_id TEXT PRIMARY KEY, authorized INTEGER NOT NULL DEFAULT 0, awaiting_password INTEGER NOT NULL DEFAULT 0, authorized_at TEXT)")
        conn.execute("INSERT INTO telegram_auth(chat_id, authorized) VALUES (?, 1)", (CHAT,))
    _run(db)
    with sqlite3.connect(db) as conn:
        assert ta.authorized_chat_ids(conn, "segredo") == [CHAT]
        assert ta.authorized_chat_ids(conn, "outra") == []


def test_failing_update_does_not_replay_the_batch(tmp_path, tg, monkeypatch):
    db = tmp_path / "m.sqlite3"
    tg.say("/start")
    tg.say("segredo")
    _run(db)

    def boom(_):
        raise RuntimeError("csv corrompido")

    monkeypatch.setattr(ta, "_status_text", boom)
    tg.say("/status")
    tg.say("/start")
    first = _run(db)
    assert first["errors"] == 1
    assert first["processed"] == 2
    sent_before = len(tg.sent())
    second = _run(db)
    assert second["processed"] == 0
    assert len(tg.sent()) == sent_before


def test_notifier_uses_valid_sessions_and_chain_aware_keys(tmp_path, monkeypatch):
    db = tmp_path / "m.sqlite3"
    conn = ta._db(db)
    now = int(time.time())
    fp = ta.password_fingerprint("segredo")
    conn.execute("INSERT INTO telegram_auth(chat_id, authorized, password_fp, authorized_epoch) VALUES ('ok', 1, ?, ?)", (fp, now))
    conn.execute("INSERT INTO telegram_auth(chat_id, authorized, password_fp, authorized_epoch) VALUES ('old', 1, 'senha-antiga', ?)", (now,))
    conn.commit()
    conn.close()

    evm = "0x" + "cd" * 20
    stage = tmp_path / "stage.csv"
    pd.DataFrame([
        {"address": evm, "chain": "base", "selective_alpha_score": 80, "selective_deep_dive_candidate": True},
        {"address": evm, "chain": "robinhood", "selective_alpha_score": 75, "selective_deep_dive_candidate": True},
        {"address": "0x" + "ef" * 20, "selective_input_source": "legacy_v6", "selective_alpha_score": 72, "selective_deep_dive_candidate": True},
    ]).to_csv(stage, index=False)

    messages = []
    monkeypatch.setattr(tn, "_send", lambda token, chat, text, timeout: messages.append((chat, text)) or {"ok": True})
    result = tn.notify_alpha_wallets(stage, db, token="T", access_password="segredo")
    assert result["recipients"] == 1 and result["sent"] == 3
    assert {chat for chat, _ in messages} == {"ok"}
    assert sum("Robinhood" in text for _, text in messages) == 2
    assert not any("Solana" in text for _, text in messages)

    again = tn.notify_alpha_wallets(stage, db, token="T", access_password="segredo")
    assert again["sent"] == 0 and again["duplicates_skipped"] == 3


def test_gmgn_cli_gets_minimal_environment(monkeypatch):
    from peixao import gmgn_live

    monkeypatch.setenv("NANSEN_API_KEY", "nansen-secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = gmgn_live._cli_env("gmgn-key")
    assert env["GMGN_API_KEY"] == "gmgn-key"
    assert env["PATH"] == "/usr/bin"
    assert "NANSEN_API_KEY" not in env and "TELEGRAM_BOT_TOKEN" not in env
