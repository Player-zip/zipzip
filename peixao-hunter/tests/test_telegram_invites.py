import sqlite3
import time

import pytest

from peixao import telegram_auth as ta
from test_telegram_security import FakeTelegram

ADMIN = "900"
USER = "111"


@pytest.fixture()
def tg(monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setattr(ta, "_api", fake.api)
    return fake


def _run(db, **kwargs):
    params = dict(token="T", access_password="segredo", db_path=db, admin_chat_ids=(ADMIN,))
    params.update(kwargs)
    return ta.process_auth_updates(**params)


def _invite_from(tg):
    text = next(t for t in reversed(tg.sent()) if "Convite criado" in t)
    return next(line for line in text.splitlines() if line.startswith("PX-"))


def test_admin_invite_grants_individual_access_with_plan(tmp_path, tg):
    db = tmp_path / "m.sqlite3"
    tg.say("/convite basico 30", chat=ADMIN)
    _run(db)
    code = _invite_from(tg)

    tg.say("/start", chat=USER)
    tg.say(code, chat=USER)
    result = _run(db)
    assert result["authorized"] == 1
    with sqlite3.connect(db) as conn:
        assert USER in ta.authorized_chat_ids(conn, "segredo")
        # plano básico não recebe alertas automáticos
        assert USER not in ta.authorized_chat_ids(conn, "segredo", plans=ta.ALERT_PLANS)
        # convite não depende da senha: trocar a senha não derruba o acesso
        assert USER in ta.authorized_chat_ids(conn, "senha-nova")
        assert ADMIN in ta.authorized_chat_ids(conn, "senha-nova", plans=ta.ALERT_PLANS)

    # código é de uso único
    tg.say("/start", chat="222")
    tg.say(code, chat="222")
    _run(db)
    with sqlite3.connect(db) as conn:
        assert "222" not in ta.authorized_chat_ids(conn, "segredo")


def test_admin_revokes_and_non_admin_is_refused(tmp_path, tg):
    db = tmp_path / "m.sqlite3"
    tg.say("/convite pro 0", chat=ADMIN)
    _run(db)
    code = _invite_from(tg)
    tg.say("/start", chat=USER)
    tg.say(code, chat=USER)
    tg.say("/convite pro 30", chat=USER)
    _run(db)
    assert "restrito a administradores" in tg.sent()[-1]

    tg.say(f"/revogar {USER}", chat=ADMIN)
    tg.say("/usuarios", chat=ADMIN)
    _run(db)
    assert any("revogado" in t for t in tg.sent())
    assert any("ACESSOS" in t and USER in t for t in tg.sent())
    with sqlite3.connect(db) as conn:
        assert USER not in ta.authorized_chat_ids(conn, "segredo")


def test_invite_access_expires(tmp_path, tg):
    db = tmp_path / "m.sqlite3"
    conn = ta._db(db)
    code = ta.create_invite(conn, plan="pro", access_days=1, created_by=ADMIN)
    conn.close()
    tg.say("/start", chat=USER)
    tg.say(code, chat=USER)
    _run(db)
    with sqlite3.connect(db) as conn:
        assert USER in ta.authorized_chat_ids(conn, "segredo")
        assert USER not in ta.authorized_chat_ids(conn, "segredo", now=int(time.time()) + 2 * 86400)


def test_removed_admin_loses_access_and_password_login_can_be_disabled(tmp_path, tg):
    db = tmp_path / "m.sqlite3"
    _run(db)
    _run(db, admin_chat_ids=())
    with sqlite3.connect(db) as conn:
        assert ADMIN not in ta.authorized_chat_ids(conn, "segredo")

    tg.say("/start", chat=USER)
    tg.say("segredo", chat=USER)
    _run(db, password_login=False)
    with sqlite3.connect(db) as conn:
        assert USER not in ta.authorized_chat_ids(conn, None)
    assert "código de convite" in [t for t in tg.sent() if "Raullux Alpha Bot" in t][-1]
