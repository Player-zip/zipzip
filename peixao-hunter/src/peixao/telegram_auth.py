from __future__ import annotations

import functools
import hashlib
import hmac
import json
import logging
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


_AUTH_COLUMNS = {
    "failed_attempts": "INTEGER NOT NULL DEFAULT 0",
    "locked_until": "INTEGER NOT NULL DEFAULT 0",
    "password_fp": "TEXT",
    "authorized_epoch": "INTEGER",
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

    Trocar TELEGRAM_ACCESS_PASSWORD muda a impressão e revoga todas as sessões.
    PBKDF2 para que o valor no banco não sirva de atalho para descobrir a senha.
    """
    digest = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), b"peixao-telegram-auth-v1", 200_000)
    return digest.hex()[:32]


def _password_matches(candidate: str, password: str) -> bool:
    return hmac.compare_digest(
        hashlib.sha256(candidate.encode("utf-8")).digest(),
        hashlib.sha256(password.encode("utf-8")).digest(),
    )


def _auth_valid(row, fingerprint: str | None, ttl_days: float, now: int) -> bool:
    """row = (authorized, password_fp, authorized_epoch)."""
    if not row or not row[0]:
        return False
    if fingerprint is not None and row[1] != fingerprint:
        return False
    if ttl_days and ttl_days > 0:
        authorized_epoch = int(row[2] or 0)
        if now - authorized_epoch > float(ttl_days) * 86400:
            return False
    return True


def authorized_chat_ids(
    conn: sqlite3.Connection,
    access_password: str | None = None,
    *,
    ttl_days: float = 0,
    now: int | None = None,
) -> list[str]:
    """Chats com sessão válida: autorizados, com a senha vigente e dentro da validade."""
    ensure_auth_schema(conn)
    now = int(time.time()) if now is None else int(now)
    fingerprint = password_fingerprint(access_password) if access_password else None
    rows = conn.execute(
        "SELECT chat_id, authorized, password_fp, authorized_epoch FROM telegram_auth WHERE authorized=1"
    ).fetchall()
    return [str(r[0]) for r in rows if _auth_valid(r[1:], fingerprint, ttl_days, now)]


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


def _locked_text(seconds_left: int) -> str:
    minutes = max(1, int(round(seconds_left / 60.0)))
    return f"🔒 Muitas tentativas erradas. Tente novamente em {minutes} min."


def _delete_message(token, chat_id, message_id, timeout) -> None:
    # A senha não deve ficar no histórico do chat. Bots podem apagar mensagens
    # recebidas em chats privados; falha aqui não bloqueia o login.
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


def _handle_message(
    conn,
    *,
    token,
    access_password: str,
    fingerprint: str,
    chat_id: str,
    text: str,
    message_id,
    db_path: Path,
    timeout: float,
    max_attempts: int,
    lockout_seconds: int,
    auth_ttl_days: float,
    now: int,
) -> str:
    """Trata uma mensagem e devolve o desfecho (para contagem)."""
    row = conn.execute(
        """
        SELECT authorized, password_fp, authorized_epoch, awaiting_password,
               failed_attempts, locked_until
        FROM telegram_auth WHERE chat_id=?
        """,
        (chat_id,),
    ).fetchone()
    is_auth = _auth_valid(row[:3] if row else None, fingerprint, auth_ttl_days, now)
    awaiting = bool(row and row[3])
    failed = int(row[4] or 0) if row else 0
    locked_until = int(row[5] or 0) if row else 0
    command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""

    if command == "/start":
        if is_auth:
            _send(token, chat_id, "🐟 Raullux Alpha Bot\n\n✅ Acesso já autorizado.\n\n" + _AUTHORIZED_HELP, timeout)
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
        _send(token, chat_id, "🔐 Raullux Alpha Bot\n\nDigite a senha de acesso.", timeout)
        return "prompted"

    if command == "/logout":
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

    if awaiting and not is_auth and not command:
        _delete_message(token, chat_id, message_id, timeout)
        if locked_until > now:
            _send(token, chat_id, _locked_text(locked_until - now), timeout)
            return "locked"
        if _password_matches(text, access_password):
            conn.execute(
                """
                UPDATE telegram_auth
                SET authorized=1, awaiting_password=0, failed_attempts=0, locked_until=0,
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
            "❌ Senha errada.\n\nPara solicitar acesso, entre em contato com @RaulLux",
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
):
    if not token or not access_password:
        return {
            "status": "NOT_CONFIGURED",
            "processed": 0,
        }

    fingerprint = password_fingerprint(access_password)
    conn = _db(db_path)

    try:
        # Sessões criadas antes da impressão de senha ficam presas à senha atual;
        # a próxima troca de senha as revoga como as demais.
        conn.execute(
            """
            UPDATE telegram_auth
            SET password_fp=?, authorized_epoch=COALESCE(authorized_epoch, CAST(strftime('%s','now') AS INTEGER))
            WHERE authorized=1 AND password_fp IS NULL
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
                        access_password=str(access_password),
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
