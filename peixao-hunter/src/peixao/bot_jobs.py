"""Fila de jobs criados pelo bot e executados pelo worker.

O Telegram só registra o pedido. O worker executa entre os ciclos (nunca em
paralelo com eles, porque escreve os mesmos caches) e responde ao admin.

- ``/acelerar <rede> [n] [extra]``: consulta até ``n`` wallets paradas da
  rede. Mostra a estimativa de custo e só roda depois de ``/confirmar``.
  Sem ``extra``, respeita o teto diário; com ``extra``, autoriza passar do
  teto só para este job.
- ``/checar <endereço> [rede]``: consulta uma wallet específica agora.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .backlog import CHAINS, classify_waiting, load_final_table, select_for_boost
from .cost_control import UNITS_PER_WALLET, record_spend, remaining_today
from .evidence_ledger import infer_chain, normalize_address
from .state import logger

CONFIRM_TTL_SECONDS = 600
MAX_BOOST_WALLETS = 100
DEFAULT_BOOST_WALLETS = 20
CHAIN_ALIASES = {"sol": "solana", "solana": "solana", "base": "base", "rh": "robinhood", "robinhood": "robinhood"}


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_jobs (
            job_id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL,
            chain TEXT NOT NULL,
            address TEXT,
            quantity INTEGER NOT NULL DEFAULT 0,
            extra INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            requested_by TEXT NOT NULL,
            created_epoch INTEGER NOT NULL,
            confirmed_epoch INTEGER,
            started_epoch INTEGER,
            finished_epoch INTEGER,
            estimate_json TEXT,
            result_json TEXT
        )
        """
    )
    conn.commit()
    return conn


def _row_to_job(row) -> dict:
    keys = (
        "job_id", "code", "kind", "chain", "address", "quantity", "extra", "status", "requested_by",
        "created_epoch", "confirmed_epoch", "started_epoch", "finished_epoch", "estimate_json", "result_json",
    )
    job = dict(zip(keys, row, strict=True))
    for key in ("estimate_json", "result_json"):
        try:
            job[key.removesuffix("_json")] = json.loads(job[key] or "{}")
        except ValueError:
            job[key.removesuffix("_json")] = {}
    return job


_JOB_SQL = (
    "SELECT job_id, code, kind, chain, address, quantity, extra, status, requested_by, created_epoch, "
    "confirmed_epoch, started_epoch, finished_epoch, estimate_json, result_json FROM bot_jobs"
)


def normalize_chain_arg(value: str | None) -> str | None:
    return CHAIN_ALIASES.get(str(value or "").strip().lower())


# ---------------------------------------------------------------------------
# Estimativa
# ---------------------------------------------------------------------------

def _providers_for(cfg, chain: str) -> list[str]:
    if chain == "solana":
        return ["BIRDEYE"] if cfg.birdeye_api_key else []
    providers = []
    if cfg.nansen_api_key:
        providers.append("NANSEN")
    if cfg.zerion_api_key:
        providers.append("ZERION")
    return providers


def _nansen_paused(cfg, chain: str) -> bool:
    path = cfg.state_dir / f"nansen_provider_{chain}_state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except ValueError:
        state = {}
    return int(state.get("cooldown_until", 0) or 0) > int(time.time())


def estimate_boost(cfg, chain: str, quantity: int, *, extra: bool) -> dict:
    wallets = select_for_boost(cfg, chain, quantity)
    providers = _providers_for(cfg, chain)
    plan = []
    for provider in providers:
        if provider == "NANSEN" and _nansen_paused(cfg, chain):
            plan.append({"provider": provider, "paused": True, "wallets": 0, "units": 0})
            continue
        per_wallet = UNITS_PER_WALLET.get(provider, 2.0)
        remaining = remaining_today(cfg.master_db, provider)
        allowed = len(wallets) if (extra or remaining is None) else min(len(wallets), int(remaining // per_wallet))
        plan.append({
            "provider": provider,
            "paused": False,
            "wallets": int(allowed),
            "units": round(allowed * per_wallet, 1),
            "remaining_today": None if remaining is None else round(remaining, 1),
        })
    usable = [p for p in plan if not p["paused"]]
    return {
        "chain": chain,
        "requested": int(quantity),
        "selected": len(wallets),
        "providers": plan,
        "max_wallets": max((p["wallets"] for p in usable), default=0),
        "extra": bool(extra),
    }


def format_estimate(estimate: dict, code: str) -> str:
    chain = estimate["chain"]
    lines = [f"⚡ ACELERAR {chain.upper()}"]
    if not estimate["selected"]:
        lines.append("Nenhuma wallet acelerável agora (veja /gargalo).")
        return "\n".join(lines)
    lines.append(f"Wallets selecionadas: {estimate['selected']} (nunca consultadas primeiro, por prioridade)")
    if not estimate["providers"]:
        lines.append("⚠️ Nenhum provedor configurado para esta rede.")
        return "\n".join(lines)
    for item in estimate["providers"]:
        name = item["provider"].title()
        if item["paused"]:
            lines.append(f"• {name}: pausado (cooldown) — não será usado")
            continue
        budget = "sem teto" if item.get("remaining_today") is None else f"restam {item['remaining_today']:g} hoje"
        lines.append(f"• {name}: até {item['wallets']} wallets ≈ {item['units']:g} unidades ({budget})")
    lines.append("O 2º provedor só é usado nas wallets que o 1º não resolver.")
    if estimate["max_wallets"] < estimate["selected"] and not estimate["extra"]:
        lines.append(
            f"O teto de hoje cobre {estimate['max_wallets']} de {estimate['selected']}. "
            f"Para passar do teto só neste job: /acelerar {chain} {estimate['selected']} extra"
        )
    if estimate["max_wallets"] <= 0:
        lines.append("Sem orçamento disponível hoje.")
        return "\n".join(lines)
    lines.append("")
    lines.append(f"Confirme em até {CONFIRM_TTL_SECONDS // 60} min: /confirmar {code}")
    lines.append(f"Desistir: /cancelar {code}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Criação / confirmação
# ---------------------------------------------------------------------------

def _new_code() -> str:
    return f"{secrets.randbelow(9000) + 1000}"


def create_boost_job(cfg, *, chain: str, quantity: int, extra: bool, requested_by: str) -> tuple[dict, dict]:
    quantity = max(1, min(MAX_BOOST_WALLETS, int(quantity)))
    estimate = estimate_boost(cfg, chain, quantity, extra=extra)
    conn = _connect(cfg.master_db)
    try:
        for _ in range(5):
            code = _new_code()
            try:
                conn.execute(
                    """
                    INSERT INTO bot_jobs(code, kind, chain, quantity, extra, status, requested_by, created_epoch, estimate_json)
                    VALUES (?, 'boost', ?, ?, ?, 'PENDING_CONFIRM', ?, ?, ?)
                    """,
                    (code, chain, quantity, int(bool(extra)), str(requested_by), int(time.time()), json.dumps(estimate)),
                )
                conn.commit()
                break
            except sqlite3.IntegrityError:
                continue
        job = _row_to_job(conn.execute(_JOB_SQL + " WHERE code=?", (code,)).fetchone())
    finally:
        conn.close()
    return job, estimate


def create_wallet_job(cfg, *, chain: str, address: str, requested_by: str) -> dict:
    conn = _connect(cfg.master_db)
    try:
        code = _new_code()
        conn.execute(
            """
            INSERT INTO bot_jobs(code, kind, chain, address, quantity, status, requested_by, created_epoch, confirmed_epoch)
            VALUES (?, 'wallet', ?, ?, 1, 'QUEUED', ?, ?, ?)
            """,
            (code, chain, address, str(requested_by), int(time.time()), int(time.time())),
        )
        conn.commit()
        return _row_to_job(conn.execute(_JOB_SQL + " WHERE code=?", (code,)).fetchone())
    finally:
        conn.close()


def confirm_job(cfg, code: str, chat_id: str) -> tuple[bool, str]:
    conn = _connect(cfg.master_db)
    try:
        row = conn.execute(_JOB_SQL + " WHERE code=?", (str(code).strip(),)).fetchone()
        if not row:
            return False, "Código não encontrado."
        job = _row_to_job(row)
        if job["requested_by"] != str(chat_id):
            return False, "Só quem pediu pode confirmar este job."
        if job["status"] != "PENDING_CONFIRM":
            return False, f"Job {code} já está {job['status']}."
        if int(time.time()) - int(job["created_epoch"]) > CONFIRM_TTL_SECONDS:
            conn.execute("UPDATE bot_jobs SET status='EXPIRED' WHERE job_id=?", (job["job_id"],))
            conn.commit()
            return False, "Confirmação expirada. Peça de novo com /acelerar."
        conn.execute("UPDATE bot_jobs SET status='QUEUED', confirmed_epoch=? WHERE job_id=?", (int(time.time()), job["job_id"]))
        conn.commit()
        return True, f"✅ Job {code} na fila. Roda assim que o ciclo atual terminar; aviso aqui com o resultado."
    finally:
        conn.close()


def cancel_job(cfg, code: str, chat_id: str) -> tuple[bool, str]:
    conn = _connect(cfg.master_db)
    try:
        row = conn.execute(_JOB_SQL + " WHERE code=?", (str(code).strip(),)).fetchone()
        if not row:
            return False, "Código não encontrado."
        job = _row_to_job(row)
        if job["requested_by"] != str(chat_id):
            return False, "Só quem pediu pode cancelar este job."
        if job["status"] not in {"PENDING_CONFIRM", "QUEUED"}:
            return False, f"Job {code} já está {job['status']}."
        conn.execute("UPDATE bot_jobs SET status='CANCELLED', finished_epoch=? WHERE job_id=?", (int(time.time()), job["job_id"]))
        conn.commit()
        return True, f"Job {code} cancelado."
    finally:
        conn.close()


def recent_jobs_text(cfg, limit: int = 8) -> str:
    conn = _connect(cfg.master_db)
    try:
        rows = conn.execute(_JOB_SQL + " ORDER BY job_id DESC LIMIT ?", (int(limit),)).fetchall()
    finally:
        conn.close()
    if not rows:
        return "Nenhum job ainda."
    lines = ["🧾 JOBS RECENTES"]
    for row in rows:
        job = _row_to_job(row)
        target = job["address"] or f"{job['quantity']} wallets"
        when = time.strftime("%d/%m %H:%M", time.gmtime(int(job["created_epoch"])))
        lines.append(f"{job['code']} · {job['kind']} {job['chain']} · {target} · {job['status']} · {when} UTC")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Execução (worker)
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _budget_wallets(cfg, provider: str, wanted: int, extra: bool) -> int:
    remaining = remaining_today(cfg.master_db, provider)
    if extra or remaining is None:
        return int(wanted)
    return max(0, min(int(wanted), int(remaining // UNITS_PER_WALLET.get(provider, 2.0))))


def _enrich_evm(cfg, chain: str, addresses: list[str], *, extra: bool, force: bool) -> dict:
    from .priority_enrichment import enrich_nansen_pnl_priority, zerion_fallback

    with tempfile.TemporaryDirectory(dir=str(cfg.state_dir)) as tmp:
        rows_path = Path(tmp) / "boost.csv"
        pd.DataFrame([{"address": a, "chain": chain} for a in addresses]).to_csv(rows_path, index=False)
        ttl = 0 if force else cfg.nansen_ttl_seconds
        retry = 0 if force else cfg.nansen_retry_seconds
        no_data = 0 if force else cfg.no_data_retry_seconds
        nansen = {"status": "SKIPPED", "http_calls": 0}
        if cfg.nansen_api_key:
            nansen = enrich_nansen_pnl_priority(
                rows_path, rows_path, cfg.state_dir,
                api_key=cfg.nansen_api_key, chain=chain, timeout=max(cfg.rpc_timeout, 15.0),
                ttl_seconds=ttl, max_wallets=_budget_wallets(cfg, "NANSEN", len(addresses), extra),
                retry_seconds=retry, no_data_ttl_seconds=no_data,
            )
            record_spend(cfg.master_db, "NANSEN", float(nansen.get("http_calls", 0) or 0), chain=chain, stage="bot_acelerar")
        zerion = {"status": "SKIPPED", "http_calls": 0}
        if cfg.zerion_api_key:
            zerion = zerion_fallback(
                rows_path, cfg.state_dir,
                api_key=cfg.zerion_api_key, chain=chain,
                max_wallets=_budget_wallets(cfg, "ZERION", len(addresses), extra),
                timeout=max(cfg.rpc_timeout, 15.0), ttl_seconds=ttl, retry_seconds=retry, no_data_ttl_seconds=no_data,
            )
            record_spend(cfg.master_db, "ZERION", float(zerion.get("http_calls", 0) or 0), chain=chain, stage="bot_acelerar")
    return {
        "nansen_calls": int(nansen.get("http_calls", 0) or 0),
        "nansen_status": nansen.get("status"),
        "zerion_calls": int(zerion.get("http_calls", 0) or 0),
        "zerion_status": zerion.get("status"),
    }


def _enrich_solana(cfg, addresses: list[str], *, extra: bool, force: bool) -> dict:
    import requests

    from .birdeye_alpha import _atomic_json, _fetch_pnl
    from .birdeye_alpha import _load_json as _birdeye_load

    if not cfg.birdeye_api_key:
        return {"birdeye_calls": 0, "birdeye_status": "NOT_CONFIGURED"}
    cache_path = cfg.state_dir / "birdeye_pnl_cache.json"
    cache = _birdeye_load(cache_path)
    entries = cache.get("wallets") if isinstance(cache.get("wallets"), dict) else {}
    limit = _budget_wallets(cfg, "BIRDEYE", len(addresses), extra)
    headers = {"X-API-KEY": cfg.birdeye_api_key, "x-chain": "solana", "Accept": "application/json"}
    session = requests.Session()
    now = int(time.time())
    calls = attempted = 0
    for wallet in addresses:
        cached = entries.get(wallet) if isinstance(entries.get(wallet), dict) else {}
        fresh = cached.get("metrics") and now - int(cached.get("checked_epoch", 0) or 0) < cfg.birdeye_pnl_ttl_seconds
        if fresh and not force:
            continue
        if attempted >= limit:
            break
        attempted += 1
        metrics, used, http_status = _fetch_pnl(session, cfg.birdeye_base_url, wallet, headers, timeout=cfg.birdeye_alpha_timeout)
        calls += int(used)
        entries[wallet] = {"checked_epoch": now, "checked_at": _now_iso(), "metrics": metrics, "http_status": http_status}
        if cfg.birdeye_alpha_delay > 0:
            time.sleep(cfg.birdeye_alpha_delay)
    _atomic_json(cache_path, {**cache, "wallets": entries})
    record_spend(cfg.master_db, "BIRDEYE", float(calls), chain="solana", stage="bot_acelerar")
    return {"birdeye_calls": int(calls), "birdeye_status": "DONE", "attempted": attempted}


def _wallet_outcomes(cfg, keys: list[str]) -> dict:
    final = load_final_table(cfg.output_dir)
    summary = {"with_wr": 0, "tiers": {}, "rows": {}}
    if final.empty:
        return summary
    wanted = set(keys)
    for row in final.to_dict("records"):
        chain = infer_chain(row)
        key = f"{chain}:{normalize_address(chain, str(row.get('address') or ''))}"
        if key not in wanted:
            continue
        summary["rows"][key] = row
        wr = row.get("win_rate")
        if wr is not None and not (isinstance(wr, float) and pd.isna(wr)):
            summary["with_wr"] += 1
        tier = str(row.get("selective_alpha_tier") or "UNENRICHED")
        summary["tiers"][tier] = summary["tiers"].get(tier, 0) + 1
    return summary


def run_job(cfg, job: dict) -> dict:
    """Executa um job e reconstrói a tabela final (sem nova chamada de API)."""
    from .final_stage import build_final_stage1
    from .telegram_notifier import notify_alpha_wallets

    chain = job["chain"]
    extra = bool(job.get("extra"))
    force = job["kind"] == "wallet"
    if job["kind"] == "wallet":
        targets = [job["address"]]
    else:
        targets = [w["address"] for w in select_for_boost(cfg, chain, int(job["quantity"]))]
    keys = [f"{chain}:{normalize_address(chain, a)}" for a in targets]
    before = classify_waiting(cfg)
    if chain == "solana":
        spent = _enrich_solana(cfg, targets, extra=extra, force=force)
    else:
        spent = _enrich_evm(cfg, chain, [a.lower() for a in targets], extra=extra, force=force)
    final = build_final_stage1(cfg)
    alerts = {"status": "SKIPPED"}
    if final.get("status") not in {"ERROR", "NO_BASELINE"}:
        alerts = notify_alpha_wallets(
            cfg.output_dir / "V22S_wallet_stage1.csv", cfg.master_db,
            token=cfg.telegram_bot_token, chat_id=cfg.telegram_chat_id, enabled=cfg.telegram_alerts_enabled,
            timeout=cfg.telegram_timeout, access_password=cfg.telegram_access_password,
            auth_ttl_days=cfg.telegram_auth_ttl_days,
        )
    outcome = _wallet_outcomes(cfg, keys)
    after = classify_waiting(cfg)
    still = after[after["wallet_key"].isin(keys)] if not after.empty else after
    return {
        "targets": len(targets),
        "with_win_rate": outcome["with_wr"],
        "tiers": outcome["tiers"],
        "still_waiting": {str(k): int(v) for k, v in (still["reason"].value_counts().to_dict() if not still.empty else {}).items()},
        "spent": spent,
        "final_stage": final.get("status"),
        "alerts_sent": int(alerts.get("sent", 0) or 0),
        "waiting_before": int(len(before[before["chain"].eq(chain)])) if not before.empty else 0,
        "waiting_after": int(len(after[after["chain"].eq(chain)])) if not after.empty else 0,
        "wallet_row": outcome["rows"].get(keys[0]) if job["kind"] == "wallet" and keys else None,
    }


def format_result(job: dict, result: dict) -> str:
    spent = result.get("spent") or {}
    cost_parts = [
        f"{name} {int(spent.get(f'{name.lower()}_calls', 0))} chamadas"
        for name in ("Nansen", "Zerion", "Birdeye") if spent.get(f"{name.lower()}_calls")
    ]
    if job["kind"] == "wallet":
        row = result.get("wallet_row") or {}
        lines = [f"🔍 CHECAGEM {job['chain'].upper()}", str(job["address"])]
        if row:
            wr = row.get("win_rate")
            wr_text = "n/d" if wr is None or (isinstance(wr, float) and pd.isna(wr)) else f"{100 * float(wr):.1f}%"
            lines.append(f"Tier: {row.get('selective_alpha_tier', 'n/d')} · score {row.get('selective_alpha_score', 'n/d')}")
            lines.append(f"Win rate: {wr_text} · posições fechadas: {row.get('closed_positions', 'n/d')} · PnL 30d: {row.get('realized_profit_30d', 'n/d')}")
        else:
            lines.append("A wallet não está na tabela final (precisa entrar pelo discovery primeiro).")
        if result.get("still_waiting"):
            reason = next(iter(result["still_waiting"]))
            lines.append(f"Ainda sem win rate: {reason.replace('_', ' ')}")
    else:
        lines = [
            f"⚡ ACELERAR {job['chain'].upper()} — concluído (job {job['code']})",
            f"Consultadas: {result.get('targets', 0)} · ganharam win rate: {result.get('with_win_rate', 0)}",
            f"Aguardando na rede: {result.get('waiting_before', 0)} → {result.get('waiting_after', 0)}",
        ]
        tiers = result.get("tiers") or {}
        if tiers:
            lines.append("Tiers: " + " · ".join(f"{k} {v}" for k, v in sorted(tiers.items())))
        still = result.get("still_waiting") or {}
        if still:
            lines.append("Continuam sem win rate: " + " · ".join(f"{v} {k.replace('_', ' ')}" for k, v in still.items()))
    lines.append("Custo: " + (", ".join(cost_parts) if cost_parts else "nenhuma chamada paga"))
    if result.get("alerts_sent"):
        lines.append(f"🚨 {result['alerts_sent']} alerta(s) enviados.")
    return "\n".join(lines)


def run_next_job(cfg, notify=None) -> dict | None:
    """Roda o próximo job confirmado. Chamado pelo worker entre os ciclos."""
    conn = _connect(cfg.master_db)
    try:
        # Pedidos sem confirmação expiram.
        conn.execute(
            "UPDATE bot_jobs SET status='EXPIRED' WHERE status='PENDING_CONFIRM' AND created_epoch < ?",
            (int(time.time()) - CONFIRM_TTL_SECONDS,),
        )
        row = conn.execute(_JOB_SQL + " WHERE status='QUEUED' ORDER BY job_id LIMIT 1").fetchone()
        if not row:
            conn.commit()
            return None
        job = _row_to_job(row)
        conn.execute("UPDATE bot_jobs SET status='RUNNING', started_epoch=? WHERE job_id=?", (int(time.time()), job["job_id"]))
        conn.commit()
    finally:
        conn.close()

    try:
        result = run_job(cfg, job)
        status = "DONE"
        text = format_result(job, result)
    except Exception as exc:
        logger.exception("job %s do bot falhou", job["code"])
        result = {"error": f"{type(exc).__name__}: {exc}"}
        status = "ERROR"
        text = f"❌ Job {job['code']} falhou: {type(exc).__name__}"
    stored = {k: v for k, v in result.items() if k != "wallet_row"}
    conn = _connect(cfg.master_db)
    try:
        conn.execute(
            "UPDATE bot_jobs SET status=?, finished_epoch=?, result_json=? WHERE job_id=?",
            (status, int(time.time()), json.dumps(stored, default=str), job["job_id"]),
        )
        conn.commit()
    finally:
        conn.close()
    if notify is not None:
        try:
            notify(job["requested_by"], text)
        except Exception:
            logger.exception("falha ao avisar o resultado do job %s", job["code"])
    return {"job": job["code"], "status": status, **stored}


def chain_of_address(cfg, address: str, explicit: str | None = None) -> tuple[str | None, str]:
    """Rede de um endereço para /checar (usa a tabela final quando possível)."""
    if explicit:
        chain = normalize_chain_arg(explicit)
        return (chain, "") if chain else (None, "Rede inválida. Use solana, base ou robinhood.")
    if not address.lower().startswith("0x"):
        return "solana", ""
    final = load_final_table(cfg.output_dir)
    chains = set()
    if not final.empty:
        for row in final.to_dict("records"):
            if str(row.get("address") or "").strip().lower() == address.lower():
                chain = infer_chain(row)
                if chain in CHAINS:
                    chains.add(chain)
    if len(chains) == 1:
        return chains.pop(), ""
    return None, "Endereço EVM: informe a rede. Ex.: /checar 0x... base"
