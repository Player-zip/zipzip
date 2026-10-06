from __future__ import annotations

import functools
import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

from .evidence_ledger import infer_chain
from .status_v23 import enhance_status_text

logger = logging.getLogger("peixao.telegram")


def _api(token, method, params=None, timeout=10.0):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {"ok": False}


def _send(token, chat_id, text, timeout):
    return _api(
        token,
        "sendMessage",
        {"chat_id": str(chat_id), "text": text},
        timeout,
    )


def _send_chunks(token, chat_id, text, timeout, max_chars=3500):
    text = str(text or "")
    if not text:
        return []

    chunks = []
    current = []
    current_len = 0

    for line in text.splitlines():
        piece = line + "\n"
        if current and current_len + len(piece) > max_chars:
            chunks.append("".join(current).rstrip())
            current = []
            current_len = 0

        if len(piece) > max_chars:
            if current:
                chunks.append("".join(current).rstrip())
                current = []
                current_len = 0
            for idx in range(0, len(piece), max_chars):
                chunks.append(piece[idx:idx + max_chars].rstrip())
            continue

        current.append(piece)
        current_len += len(piece)

    if current:
        chunks.append("".join(current).rstrip())

    results = []
    for chunk in chunks:
        results.append(_send(token, chat_id, chunk, timeout))
    return results


# Planos de acesso. "pro" recebe os alertas automáticos; "basico" só consulta.
PLANS = {
    "pro": "alertas automáticos + /status + /wallets_bs",
    "basico": "/status + /wallets_bs (sem alertas automáticos)",
}
ALERT_PLANS = ("pro",)
INVITE_CODE_TTL_DAYS = 7

_AUTH_COLUMNS = {
    "failed_attempts": "INTEGER NOT NULL DEFAULT 0",
    "locked_until": "INTEGER NOT NULL DEFAULT 0",
    "password_fp": "TEXT",
    "authorized_epoch": "INTEGER",
    "plan": "TEXT NOT NULL DEFAULT 'pro'",
    # password = senha compartilhada; invite = convite individual; admin = TELEGRAM_ADMIN_CHAT_IDS
    "auth_method": "TEXT NOT NULL DEFAULT 'password'",
    "access_expires_epoch": "INTEGER NOT NULL DEFAULT 0",
}


def ensure_auth_schema(conn: sqlite3.Connection) -> None:
    """Cria/migra as tabelas de login (também usada pelo notificador)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_auth (
            chat_id TEXT PRIMARY KEY,
            authorized INTEGER NOT NULL DEFAULT 0,
            awaiting_password INTEGER NOT NULL DEFAULT 0,
            authorized_at TEXT
        )
        """
    )
    existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(telegram_auth)").fetchall()}
    for column, ddl in _AUTH_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE telegram_auth ADD COLUMN {column} {ddl}")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_auth_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_invites (
            code_hash TEXT PRIMARY KEY,
            plan TEXT NOT NULL,
            access_days INTEGER NOT NULL DEFAULT 0,
            created_by TEXT NOT NULL,
            created_epoch INTEGER NOT NULL,
            expires_epoch INTEGER NOT NULL,
            used_by TEXT,
            used_epoch INTEGER
        )
        """
    )
    conn.commit()


def _db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    # O mesmo SQLite é escrito pelo pipeline; espera o lock em vez de falhar.
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    ensure_auth_schema(conn)
    return conn


@functools.lru_cache(maxsize=8)
def password_fingerprint(password: str) -> str:
    """Impressão da senha vigente, gravada junto da autorização.

    Trocar TELEGRAM_ACCESS_PASSWORD muda a impressão e revoga todas as sessões
    abertas por senha (convites individuais não dependem da senha).
    PBKDF2 para que o valor no banco não sirva de atalho para descobrir a senha.
    """
    digest = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), b"peixao-telegram-auth-v1", 200_000)
    return digest.hex()[:32]


def _password_matches(candidate: str, password: str) -> bool:
    return hmac.compare_digest(
        hashlib.sha256(candidate.encode("utf-8")).digest(),
        hashlib.sha256(password.encode("utf-8")).digest(),
    )


_SESSION_SQL = (
    "SELECT chat_id, authorized, password_fp, authorized_epoch, auth_method, access_expires_epoch, plan, "
    "awaiting_password, failed_attempts, locked_until FROM telegram_auth"
)
_SESSION_KEYS = (
    "chat_id", "authorized", "password_fp", "authorized_epoch", "auth_method", "access_expires_epoch", "plan",
    "awaiting_password", "failed_attempts", "locked_until",
)


def _session(conn: sqlite3.Connection, chat_id: str) -> dict | None:
    row = conn.execute(_SESSION_SQL + " WHERE chat_id=?", (str(chat_id),)).fetchone()
    return dict(zip(_SESSION_KEYS, row, strict=True)) if row else None


def _auth_valid(session: dict | None, *, fingerprint: str | None, ttl_days: float, now: int) -> bool:
    if not session or not session.get("authorized"):
        return False
    method = str(session.get("auth_method") or "password")
    if method in {"invite", "admin"}:
        expires = int(session.get("access_expires_epoch") or 0)
        return not expires or now < expires
    # Senha compartilhada: precisa da senha vigente (login por senha desligado => inválida).
    if fingerprint is None or session.get("password_fp") != fingerprint:
        return False
    if ttl_days and ttl_days > 0:
        if now - int(session.get("authorized_epoch") or 0) > float(ttl_days) * 86400:
            return False
    return True


def authorized_chat_ids(
    conn: sqlite3.Connection,
    access_password: str | None = None,
    *,
    ttl_days: float = 0,
    now: int | None = None,
    plans: tuple[str, ...] | None = None,
) -> list[str]:
    """Chats com sessão válida (e, se pedido, só dos planos indicados)."""
    ensure_auth_schema(conn)
    now = int(time.time()) if now is None else int(now)
    fingerprint = password_fingerprint(access_password) if access_password else None
    sessions = [dict(zip(_SESSION_KEYS, r, strict=True)) for r in conn.execute(_SESSION_SQL + " WHERE authorized=1").fetchall()]
    return [
        str(s["chat_id"]) for s in sessions
        if _auth_valid(s, fingerprint=fingerprint, ttl_days=ttl_days, now=now)
        and (plans is None or str(s.get("plan") or "pro") in plans)
    ]


def _invite_hash(code: str) -> str:
    return hashlib.sha256(("peixao-invite-v1:" + str(code).strip().upper()).encode("utf-8")).hexdigest()


def create_invite(
    conn: sqlite3.Connection,
    *,
    plan: str,
    access_days: int,
    created_by: str,
    now: int | None = None,
) -> str:
    """Gera um código de uso único; só o hash fica no banco."""
    if plan not in PLANS:
        raise ValueError(f"plano inválido: {plan}")
    now = int(time.time()) if now is None else int(now)
    raw = secrets.token_hex(6).upper()
    code = f"PX-{raw[:4]}-{raw[4:8]}-{raw[8:]}"
    conn.execute(
        """
        INSERT INTO telegram_invites(code_hash, plan, access_days, created_by, created_epoch, expires_epoch)
        VALUES (?,?,?,?,?,?)
        """,
        (_invite_hash(code), plan, max(0, int(access_days)), str(created_by), now, now + INVITE_CODE_TTL_DAYS * 86400),
    )
    conn.commit()
    return code


def _redeem_invite(conn: sqlite3.Connection, code: str, chat_id: str, now: int) -> dict | None:
    code_hash = _invite_hash(code)
    row = conn.execute(
        "SELECT plan, access_days FROM telegram_invites WHERE code_hash=? AND used_by IS NULL AND expires_epoch > ?",
        (code_hash, now),
    ).fetchone()
    if not row:
        return None
    updated = conn.execute(
        "UPDATE telegram_invites SET used_by=?, used_epoch=? WHERE code_hash=? AND used_by IS NULL",
        (str(chat_id), now, code_hash),
    ).rowcount
    if not updated:
        return None
    plan, days = str(row[0]), int(row[1] or 0)
    return {"plan": plan, "access_expires_epoch": now + days * 86400 if days else 0}


def _sync_admins(conn: sqlite3.Connection, admin_chat_ids: tuple[str, ...], now: int) -> None:
    admins = {str(x).strip() for x in admin_chat_ids if str(x).strip()}
    for chat_id in admins:
        conn.execute(
            """
            INSERT INTO telegram_auth(chat_id, authorized, awaiting_password, auth_method, plan, authorized_epoch, access_expires_epoch)
            VALUES (?, 1, 0, 'admin', 'pro', ?, 0)
            ON CONFLICT(chat_id) DO UPDATE SET authorized=1, awaiting_password=0, auth_method='admin',
                plan='pro', access_expires_epoch=0
            """,
            (chat_id, now),
        )
    placeholders = ",".join("?" for _ in admins) or "''"
    conn.execute(
        f"UPDATE telegram_auth SET authorized=0 WHERE auth_method='admin' AND chat_id NOT IN ({placeholders})",
        tuple(admins),
    )
    conn.commit()


def _read_csv(path: Path):
    try:
        if path.is_file():
            return pd.read_csv(path)
    except Exception:
        pass
    return pd.DataFrame()


def _score_column(frame: pd.DataFrame) -> str | None:
    if "selective_alpha_score" in frame.columns:
        return "selective_alpha_score"
    if "alpha22_score" in frame.columns:
        return "alpha22_score"
    return None


def _stage_chain_labels(stage: pd.DataFrame) -> pd.Series:
    if stage.empty:
        return pd.Series(dtype=str)
    # Mesma regra usada nos alertas (evidence_ledger.infer_chain).
    return pd.Series([infer_chain(row) for row in stage.to_dict("records")], index=stage.index, dtype="object")


def _alpha_mask(stage: pd.DataFrame, scores: pd.Series) -> pd.Series:
    if stage.empty or scores.empty:
        return pd.Series(False, index=stage.index)

    if "selective_deep_dive_candidate" in stage.columns:
        deep = (
            stage["selective_deep_dive_candidate"]
            .astype(str)
            .str.lower()
            .isin(["true", "1", "yes"])
        )
        return scores.ge(70) & deep

    if "alpha22_gate_status" in stage.columns:
        passed = (
            stage["alpha22_gate_status"]
            .astype(str)
            .str.upper()
            .eq("PASS")
        )
        return scores.ge(70) & passed

    return pd.Series(False, index=stage.index)


def _chain_wallet_stats(
    stage: pd.DataFrame,
    labels: pd.Series,
    chain: str,
    score_col: str | None,
) -> dict:
    if stage.empty:
        return {"wallets": 0, "scored": 0, "waiting": 0, "alpha": 0}

    mask = labels.eq(chain)
    subset = stage.loc[mask]
    wallets = int(len(subset))

    if not score_col or subset.empty:
        return {
            "wallets": wallets,
            "scored": 0,
            "waiting": wallets,
            "alpha": 0,
        }

    scores = pd.to_numeric(subset[score_col], errors="coerce")
    scored = int(scores.notna().sum())
    alpha = int(_alpha_mask(subset, scores).sum())
    return {
        "wallets": wallets,
        "scored": scored,
        "waiting": max(0, wallets - scored),
        "alpha": alpha,
    }


def _completed_stage(data_dir: Path) -> pd.DataFrame:
    output = data_dir / "output"
    completed = output / "V22S_wallet_stage1_completed.csv"
    live = output / "V22S_wallet_stage1.csv"
    return _read_csv(completed if completed.is_file() else live)


def _status_text(data_dir: Path) -> str:
    return enhance_status_text(_base_status_text(data_dir), data_dir)


def _base_status_text(data_dir: Path) -> str:
    output = data_dir / "output"
    state_path = data_dir / "state" / "pipeline_state.json"

    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    except Exception:
        state = {}

    stage = _completed_stage(data_dir)

    radar_by_chain = {
        "solana": _read_csv(output / "V22_token_radar.csv"),
        "base": _read_csv(output / "V22_base_token_radar.csv"),
        "robinhood": _read_csv(output / "V22_robinhood_token_radar.csv"),
    }
    shortlist_by_chain = {
        "solana": _read_csv(output / "V22_token_radar_shortlist.csv"),
        "base": _read_csv(output / "V22_base_token_radar_shortlist.csv"),
        "robinhood": _read_csv(output / "V22_robinhood_token_radar_shortlist.csv"),
    }

    score_col = _score_column(stage)
    if score_col:
        scores = pd.to_numeric(stage[score_col], errors="coerce")
    else:
        scores = pd.Series(dtype=float)

    total = int(len(stage))
    scored = int(scores.notna().sum())
    waiting = max(0, total - scored)
    alpha_mask = _alpha_mask(stage, scores) if score_col else pd.Series(False, index=stage.index)
    alpha = int(alpha_mask.sum())

    labels = _stage_chain_labels(stage)
    chain_stats = {
        chain: _chain_wallet_stats(stage, labels, chain, score_col)
        for chain in ("solana", "base", "robinhood")
    }

    def band(lo, hi=None):
        if scores.empty:
            return 0
        mask = scores.ge(lo)
        if hi is not None:
            mask &= scores.lt(hi)
        return int(mask.sum())

    gate_col = "alpha22_gate_status" if "alpha22_gate_status" in stage.columns else None
    if gate_col:
        gates = stage[gate_col].astype(str).str.upper()
        reject_wr = int(gates.eq("REJECT_WR").sum())
        low_sample = int(gates.eq("PASS_LOW_SAMPLE").sum())
    else:
        reject_wr = 0
        low_sample = 0

    if "selective_deep_dive_candidate" in stage.columns:
        deep = int(
            stage["selective_deep_dive_candidate"]
            .astype(str)
            .str.lower()
            .isin(["true", "1", "yes"])
            .sum()
        )
    elif "alpha22_deep_dive_candidate" in stage.columns:
        deep = int(
            stage["alpha22_deep_dive_candidate"]
            .astype(str)
            .str.lower()
            .isin(["true", "1", "yes"])
            .sum()
        )
    else:
        deep = 0

    tokens_seen = {chain: int(len(frame)) for chain, frame in radar_by_chain.items()}
    tokens_shortlist = {chain: int(len(frame)) for chain, frame in shortlist_by_chain.items()}
    total_tokens = sum(tokens_seen.values())
    total_shortlist = sum(tokens_shortlist.values())

    last_cycle = str(
        state.get("last_run_finished_at")
        or state.get("last_run_started_at")
        or "n/d"
    )
    run_status = str(state.get("last_run_status") or "UNKNOWN").upper()
    status_icon = "🟢" if run_status in {"DONE", "RUNNING", "OK"} else "🟡"

    def chain_block(title: str, emoji: str, chain: str) -> str:
        stats = chain_stats[chain]
        return (
            f"{emoji} {title}\n"
            f"🔎 Tokens vistos: {tokens_seen[chain]}\n"
            f"🎯 Shortlist: {tokens_shortlist[chain]}\n"
            f"🐋 Wallets acompanhadas: {stats['wallets']}\n"
            f"📐 Wallets com score: {stats['scored']}\n"
            f"⏳ Aguardando evidência: {stats['waiting']}\n"
            f"🚨 Alpha aprovado: {stats['alpha']}\n"
        )

    return (
        "🐟 RAULLUX — STATUS MULTICHAIN\n\n"
        + chain_block("SOLANA", "🟣", "solana")
        + "\n"
        + chain_block("BASE", "🔵", "base")
        + "\n"
        + chain_block("ROBINHOOD", "🟢", "robinhood")
        + "\n"
        "🌐 CONSOLIDADO\n"
        f"🔎 Tokens vistos: {total_tokens}\n"
        f"🎯 Tokens shortlist: {total_shortlist}\n"
        f"🐋 Wallets acompanhadas: {total}\n"
        f"📐 Wallets com score: {scored}\n"
        f"⏳ Aguardando evidência: {waiting}\n"
        f"🚨 Alpha aprovado: {alpha}\n\n"
        "📊 NÍVEIS DE SCORE — CONSOLIDADO\n"
        f"🔴 D  <50: {band(0, 50)}\n"
        f"🟠 C  50–59: {band(50, 60)}\n"
        f"🟡 B  60–69: {band(60, 70)}\n"
        f"🟢 A  70–79: {band(70, 80)}\n"
        f"🔵 A+ 80–89: {band(80, 90)}\n"
        f"🟣 S  90+: {band(90)}\n\n"
        "🚦 GATES — CONSOLIDADO\n"
        f"❌ Reprovadas por WR: {reject_wr}\n"
        f"⚠️ Amostra insuficiente: {low_sample}\n"
        f"🔬 Deep-dive: {deep}\n\n"
        f"🕐 Último ciclo: {last_cycle}\n"
        f"⚙️ Estado: {status_icon} {run_status}\n\n"
        "🎯 REGRA ALPHA\n"
        "B ≥60 = investigação\n"
        "A ≥70 + gates finais = ALPHA\n"
        "A+ ≥80 = Alpha forte\n"
        "S ≥90 = Alpha excepcional"
    )


def _wallet_address_column(stage: pd.DataFrame) -> str | None:
    for name in ("address", "wallet_address", "wallet"):
        if name in stage.columns:
            return name
    return None


def _score_band(score: float) -> tuple[str, str]:
    if score >= 90:
        return "S", "🟣"
    if score >= 80:
        return "A+", "🔵"
    if score >= 70:
        return "A", "🟢"
    return "B", "🟡"


def _wallets_bs_text(data_dir: Path) -> str:
    stage = _completed_stage(data_dir)
    if stage.empty:
        return "📭 Nenhuma wallet disponível no último ciclo concluído."

    score_col = _score_column(stage)
    address_col = _wallet_address_column(stage)
    if not score_col or not address_col:
        return "⚠️ O snapshot atual não contém score/endereço suficientes para extrair as wallets B–S."

    scores = pd.to_numeric(stage[score_col], errors="coerce")
    mask = scores.ge(60) & scores.notna()
    if not mask.any():
        return "📭 Nenhuma wallet com score B, A, A+ ou S no último ciclo concluído."

    subset = stage.loc[mask].copy()
    subset["_score"] = scores.loc[mask]
    subset["_chain"] = _stage_chain_labels(stage).loc[mask]
    subset["_address"] = subset[address_col].fillna("").astype(str).str.strip()
    subset = subset[subset["_address"].ne("")]
    subset = subset.sort_values("_score", ascending=False, kind="mergesort")

    if subset.empty:
        return "📭 Nenhuma wallet válida com score B–S no último ciclo concluído."

    counts = {
        "S": int(subset["_score"].ge(90).sum()),
        "A+": int((subset["_score"].ge(80) & subset["_score"].lt(90)).sum()),
        "A": int((subset["_score"].ge(70) & subset["_score"].lt(80)).sum()),
        "B": int((subset["_score"].ge(60) & subset["_score"].lt(70)).sum()),
    }

    chain_names = {
        "solana": "SOLANA",
        "base": "BASE",
        "robinhood": "ROBINHOOD",
    }

    lines = [
        "🐟 RAULLUX — WALLETS B → S",
        "",
        f"Total: {len(subset)}",
        f"🟣 S: {counts['S']} | 🔵 A+: {counts['A+']} | 🟢 A: {counts['A']} | 🟡 B: {counts['B']}",
        "",
    ]

    for idx, (_, row) in enumerate(subset.iterrows(), start=1):
        score = float(row["_score"])
        chain = str(row["_chain"] or "").lower()
        address = str(row["_address"])
        band, emoji = _score_band(score)
        lines.append(
            f"{idx}. {emoji} {band} {score:.1f} | {chain_names.get(chain, chain.upper() or 'N/D')}\n"
            f"{address}"
        )

    return "\n".join(lines)


def _blocked_text() -> str:
    return "🔒 Acesso bloqueado.\nEnvie /start para autenticar."


_AUTHORIZED_HELP = (
    "Use /status para acompanhar o radar.\n"
    "Use /wallets_bs para listar wallets B–S.\n"
    "Use /logout para encerrar a sessão."
)
_ADMIN_HELP = (
    "Administração:\n"
    "/convite [pro|basico] [dias] — gera um convite de uso único\n"
    "/usuarios — lista os acessos\n"
    "/revogar <chat_id> — revoga um acesso"
)


def _locked_text(seconds_left: int) -> str:
    minutes = max(1, int(round(seconds_left / 60.0)))
    return f"🔒 Muitas tentativas erradas. Tente novamente em {minutes} min."


def _delete_message(token, chat_id, message_id, timeout) -> None:
    # A senha/código não deve ficar no histórico do chat. Bots podem apagar
    # mensagens recebidas em chats privados; falha aqui não bloqueia o login.
    if message_id is None:
        return
    _api(token, "deleteMessage", {"chat_id": str(chat_id), "message_id": str(message_id)}, timeout)


def _save_offset(conn, offset: int) -> None:
    conn.execute(
        """
        INSERT INTO telegram_auth_state(key, value)
        VALUES('offset', ?)
        ON CONFLICT(key)
        DO UPDATE SET value=excluded.value
        """,
        (str(offset),),
    )
    conn.commit()


def _fmt_date(epoch) -> str:
    epoch = int(epoch or 0)
    if not epoch:
        return "sem expiração"
    return time.strftime("%d/%m/%Y", time.gmtime(epoch))


def _session_summary(session: dict) -> str:
    plan = str(session.get("plan") or "pro")
    method = {"invite": "convite", "admin": "admin", "password": "senha"}.get(str(session.get("auth_method")), "senha")
    expires = int(session.get("access_expires_epoch") or 0)
    validity = f"válido até {_fmt_date(expires)}" if expires else "sem expiração"
    return f"Plano: {plan} ({PLANS.get(plan, '')}) · acesso por {method} · {validity}"


def _admin_command(conn, *, token, chat_id, command, args, timeout, now) -> str:
    if command == "/convite":
        plan = (args[0].lower() if args else "pro")
        if plan not in PLANS:
            _send(token, chat_id, f"Plano inválido. Use: {', '.join(PLANS)}.", timeout)
            return "noop"
        try:
            days = int(args[1]) if len(args) > 1 else 30
        except ValueError:
            _send(token, chat_id, "Dias inválidos. Ex.: /convite pro 30 (0 = sem expiração).", timeout)
            return "noop"
        code = create_invite(conn, plan=plan, access_days=days, created_by=chat_id, now=now)
        _send(
            token,
            chat_id,
            (
                f"🎟️ Convite criado ({plan}, {'sem expiração' if days <= 0 else f'{days} dias de acesso'}).\n\n"
                f"{code}\n\n"
                f"Uso único, vale para resgatar por {INVITE_CODE_TTL_DAYS} dias. "
                "O usuário envia /start ao bot e depois o código."
            ),
            timeout,
        )
        return "invite_created"
    if command == "/usuarios":
        sessions = [dict(zip(_SESSION_KEYS, r, strict=True)) for r in conn.execute(_SESSION_SQL + " ORDER BY authorized DESC, chat_id").fetchall()]
        lines = ["👥 ACESSOS"]
        for s in sessions:
            if not s.get("authorized") and not s.get("authorized_epoch"):
                continue  # nunca entrou (só mandou /start)
            status = "✅" if s.get("authorized") else "⛔"
            lines.append(f"{status} {s['chat_id']} — {_session_summary(s)}")
        if len(lines) == 1:
            lines.append("Nenhum acesso registrado.")
        _send_chunks(token, chat_id, "\n".join(lines), timeout)
        return "noop"
    if command == "/revogar":
        target = args[0] if args else ""
        if not target:
            _send(token, chat_id, "Uso: /revogar <chat_id>", timeout)
            return "noop"
        changed = conn.execute(
            "UPDATE telegram_auth SET authorized=0, awaiting_password=0 WHERE chat_id=? AND auth_method != 'admin'",
            (target,),
        ).rowcount
        conn.commit()
        _send(token, chat_id, f"⛔ Acesso de {target} revogado." if changed else f"Nenhum acesso revogável para {target}.", timeout)
        return "revoked" if changed else "noop"
    return "noop"


def _handle_message(
    conn,
    *,
    token,
    access_password: str | None,
    fingerprint: str | None,
    chat_id: str,
    text: str,
    message_id,
    db_path: Path,
    timeout: float,
    max_attempts: int,
    lockout_seconds: int,
    auth_ttl_days: float,
    now: int,
    admin_chat_ids: tuple[str, ...] = (),
) -> str:
    """Trata uma mensagem e devolve o desfecho (para contagem)."""
    session = _session(conn, chat_id)
    is_auth = _auth_valid(session, fingerprint=fingerprint, ttl_days=auth_ttl_days, now=now)
    is_admin = is_auth and chat_id in set(admin_chat_ids)
    awaiting = bool(session and session.get("awaiting_password"))
    failed = int((session or {}).get("failed_attempts") or 0)
    locked_until = int((session or {}).get("locked_until") or 0)
    parts = text.split()
    command = parts[0].split("@")[0].lower() if text.startswith("/") else ""
    args = parts[1:] if command else []

    if command == "/start":
        if is_auth:
            extra = "\n\n" + _ADMIN_HELP if is_admin else ""
            _send(
                token,
                chat_id,
                "🐟 Raullux Alpha Bot\n\n✅ Acesso já autorizado.\n" + _session_summary(session) + "\n\n" + _AUTHORIZED_HELP + extra,
                timeout,
            )
            return "noop"
        conn.execute(
            """
            INSERT INTO telegram_auth(chat_id, authorized, awaiting_password)
            VALUES(?, 0, 1)
            ON CONFLICT(chat_id)
            DO UPDATE SET authorized=0, awaiting_password=1
            """,
            (chat_id,),
        )
        conn.commit()
        if locked_until > now:
            _send(token, chat_id, _locked_text(locked_until - now), timeout)
            return "locked"
        prompt = "Digite a senha de acesso ou o código de convite." if fingerprint else "Digite o código de convite."
        _send(token, chat_id, "🔐 Raullux Alpha Bot\n\n" + prompt, timeout)
        return "prompted"

    if command == "/logout":
        if session and session.get("auth_method") == "admin":
            _send(token, chat_id, "Admins são definidos por TELEGRAM_ADMIN_CHAT_IDS; remova o chat de lá para sair.", timeout)
            return "noop"
        conn.execute(
            "UPDATE telegram_auth SET authorized=0, awaiting_password=0, password_fp=NULL WHERE chat_id=?",
            (chat_id,),
        )
        conn.commit()
        _send(token, chat_id, "👋 Sessão encerrada. Envie /start para entrar de novo.", timeout)
        return "logout"

    if command == "/status":
        if is_auth:
            _send_chunks(token, chat_id, _status_text(db_path.parent), timeout)
            return "noop"
        _send(token, chat_id, _blocked_text(), timeout)
        return "blocked"

    if command == "/wallets_bs":
        if is_auth:
            _send_chunks(token, chat_id, _wallets_bs_text(db_path.parent), timeout)
            return "noop"
        _send(token, chat_id, _blocked_text(), timeout)
        return "blocked"

    if command in {"/convite", "/usuarios", "/revogar"}:
        if is_admin:
            return _admin_command(conn, token=token, chat_id=chat_id, command=command, args=args, timeout=timeout, now=now)
        _send(token, chat_id, _blocked_text() if not is_auth else "Comando restrito a administradores.", timeout)
        return "blocked"

    if awaiting and not is_auth and not command:
        _delete_message(token, chat_id, message_id, timeout)
        if locked_until > now:
            _send(token, chat_id, _locked_text(locked_until - now), timeout)
            return "locked"
        invite = _redeem_invite(conn, text, chat_id, now)
        if invite is not None:
            conn.execute(
                """
                UPDATE telegram_auth
                SET authorized=1, awaiting_password=0, failed_attempts=0, locked_until=0,
                    auth_method='invite', plan=?, access_expires_epoch=?, password_fp=NULL,
                    authorized_epoch=?, authorized_at=CURRENT_TIMESTAMP
                WHERE chat_id=?
                """,
                (invite["plan"], invite["access_expires_epoch"], now, chat_id),
            )
            conn.commit()
            session = _session(conn, chat_id)
            _send(token, chat_id, "✅ Convite aceito. Acesso autorizado ao Raullux Alpha Bot.\n" + _session_summary(session) + "\n\n" + _AUTHORIZED_HELP, timeout)
            return "authorized"
        if fingerprint and access_password and _password_matches(text, access_password):
            conn.execute(
                """
                UPDATE telegram_auth
                SET authorized=1, awaiting_password=0, failed_attempts=0, locked_until=0,
                    auth_method='password', plan='pro', access_expires_epoch=0,
                    password_fp=?, authorized_epoch=?, authorized_at=CURRENT_TIMESTAMP
                WHERE chat_id=?
                """,
                (fingerprint, now, chat_id),
            )
            conn.commit()
            _send(token, chat_id, "✅ Senha correta. Acesso autorizado ao Raullux Alpha Bot.\n\n" + _AUTHORIZED_HELP, timeout)
            return "authorized"
        failed += 1
        if failed >= max(1, int(max_attempts)):
            locked_until = now + max(60, int(lockout_seconds))
            conn.execute(
                "UPDATE telegram_auth SET failed_attempts=0, locked_until=? WHERE chat_id=?",
                (locked_until, chat_id),
            )
            conn.commit()
            logger.warning("login do Telegram bloqueado para o chat %s após %s tentativas", chat_id, failed)
            _send(token, chat_id, _locked_text(locked_until - now), timeout)
            return "denied"
        conn.execute("UPDATE telegram_auth SET failed_attempts=? WHERE chat_id=?", (failed, chat_id))
        conn.commit()
        _send(
            token,
            chat_id,
            "❌ Senha ou código inválido.\n\nPara solicitar acesso, entre em contato com @RaulLux",
            timeout,
        )
        return "denied"

    if not is_auth:
        _send(token, chat_id, _blocked_text(), timeout)
        return "blocked"
    return "noop"


def process_auth_updates(
    *,
    token,
    access_password,
    db_path: Path,
    timeout=10.0,
    max_attempts: int = 5,
    lockout_seconds: int = 900,
    auth_ttl_days: float = 0,
    admin_chat_ids: tuple[str, ...] = (),
    password_login: bool = True,
):
    admins = tuple(str(x).strip() for x in (admin_chat_ids or ()) if str(x).strip())
    password = str(access_password or "") if password_login else ""
    if not token or (not password and not admins):
        return {
            "status": "NOT_CONFIGURED",
            "processed": 0,
        }

    fingerprint = password_fingerprint(password) if password else None
    conn = _db(db_path)

    try:
        now = int(time.time())
        _sync_admins(conn, admins, now)
        if fingerprint:
            # Sessões por senha criadas antes da impressão ficam presas à senha
            # atual; a próxima troca de senha as revoga como as demais.
            conn.execute(
                """
                UPDATE telegram_auth
                SET password_fp=?, authorized_epoch=COALESCE(authorized_epoch, CAST(strftime('%s','now') AS INTEGER))
                WHERE authorized=1 AND password_fp IS NULL AND auth_method='password'
                """,
                (fingerprint,),
            )
            conn.commit()

        row = conn.execute(
            "SELECT value FROM telegram_auth_state WHERE key='offset'"
        ).fetchone()
        offset = int(row[0]) if row else 0

        result = _api(
            token,
            "getUpdates",
            {
                "offset": offset,
                "timeout": "0",
                "allowed_updates": json.dumps(["message"]),
            },
            timeout,
        )

        if not result.get("ok"):
            return {
                "status": "TELEGRAM_ERROR",
                "processed": 0,
            }

        updates = result.get("result") or []
        counts = {"processed": 0, "authorized": 0, "denied": 0, "locked": 0, "errors": 0}

        for upd in updates:
            update_id = int(upd.get("update_id", 0))
            # O offset avança antes do tratamento: uma mensagem que quebra o
            # handler não é reprocessada a cada 3 s (nem trava o bot).
            offset = max(offset, update_id + 1)
            try:
                msg = upd.get("message") or {}
                chat = msg.get("chat") or {}
                chat_id = str(chat.get("id", ""))
                text = str(msg.get("text", "")).strip()
                if chat_id and text:
                    counts["processed"] += 1
                    outcome = _handle_message(
                        conn,
                        token=token,
                        access_password=password or None,
                        fingerprint=fingerprint,
                        chat_id=chat_id,
                        text=text,
                        message_id=msg.get("message_id"),
                        db_path=db_path,
                        timeout=timeout,
                        max_attempts=max_attempts,
                        lockout_seconds=lockout_seconds,
                        auth_ttl_days=auth_ttl_days,
                        now=int(time.time()),
                        admin_chat_ids=admins,
                    )
                    if outcome in counts:
                        counts[outcome] += 1
            except Exception:
                counts["errors"] += 1
                logger.exception("falha ao tratar update %s do Telegram", update_id)
            finally:
                _save_offset(conn, offset)

        return {"status": "DONE" if not counts["errors"] else "PARTIAL", **counts}

    finally:
        conn.close()
