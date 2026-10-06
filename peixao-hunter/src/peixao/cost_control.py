"""Controle de gasto com APIs pagas.

Cada etapa paga registra quanto consumiu (chamadas, execuções ou créditos) e
só roda se o teto diário do provedor ainda não foi atingido. O teto é
fail-closed: atingiu, a etapa é pulada até a virada do dia (UTC). O relatório
alimenta o /status e o custo por wallet validada.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import os
import sqlite3
import time
from typing import Callable

from .config import cost_default

# Unidade de cobrança por provedor.
PROVIDER_UNITS = {
    "NANSEN": "chamadas",
    "BIRDEYE": "chamadas",
    "ZERION": "chamadas",
    "COINSTATS": "chamadas",
    "DUNE": "execuções",
    "QUICKNODE": "créditos",
    "HELIUS": "chamadas",
    "SHYFT": "chamadas",
}


def _day_key(epoch: int | None = None) -> str:
    moment = datetime.fromtimestamp(int(time.time()) if epoch is None else int(epoch), timezone.utc)
    return moment.strftime("%Y-%m-%d")


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_spend (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_epoch INTEGER NOT NULL,
            day_key TEXT NOT NULL,
            provider TEXT NOT NULL,
            chain TEXT NOT NULL DEFAULT '',
            stage TEXT NOT NULL DEFAULT '',
            units REAL NOT NULL DEFAULT 0,
            skipped INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_provider_spend_day ON provider_spend(provider, day_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_provider_spend_epoch ON provider_spend(recorded_epoch)")
    return conn


def daily_cap(provider: str) -> float:
    """Teto diário (0 = sem teto). QuickNode usa o orçamento de créditos próprio."""
    provider = str(provider).upper()
    if provider == "QUICKNODE":
        name = "PEIXAO_QUICKNODE_DAILY_CREDITS"
    else:
        name = f"PEIXAO_DAILY_CAP_{provider}"
    raw = os.getenv(name, "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return float(cost_default(name, 0))


def spent_today(db_path: Path, provider: str, *, now_epoch: int | None = None) -> float:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(units), 0) FROM provider_spend WHERE provider=? AND day_key=?",
            (str(provider).upper(), _day_key(now_epoch)),
        ).fetchone()
    finally:
        conn.close()
    return float(row[0] or 0.0)


def remaining_today(db_path: Path, provider: str, *, now_epoch: int | None = None) -> float | None:
    """Unidades restantes hoje; ``None`` quando não há teto."""
    cap = daily_cap(provider)
    if cap <= 0:
        return None
    return max(0.0, cap - spent_today(db_path, provider, now_epoch=now_epoch))


def record_spend(
    db_path: Path,
    provider: str,
    units: float,
    *,
    chain: str = "",
    stage: str = "",
    skipped: bool = False,
    now_epoch: int | None = None,
) -> None:
    epoch = int(time.time()) if now_epoch is None else int(now_epoch)
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO provider_spend(recorded_epoch, day_key, provider, chain, stage, units, skipped) VALUES (?,?,?,?,?,?,?)",
            (epoch, _day_key(epoch), str(provider).upper(), str(chain or ""), str(stage or ""), float(max(0.0, units)), int(bool(skipped))),
        )
        conn.commit()
    finally:
        conn.close()


def _as_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def run_paid_step(
    db_path: Path,
    provider: str,
    stage: str,
    fn: Callable[[float | None], dict],
    *,
    units_from: Callable[[dict], float],
    chain: str = "",
    min_units: float = 1.0,
    extra_spend: Callable[[dict], dict[str, float]] | None = None,
) -> dict:
    """Roda ``fn(remaining)`` só se houver orçamento; registra o consumo real.

    ``fn`` recebe as unidades restantes (ou ``None`` sem teto) para limitar o
    próprio lote. ``extra_spend`` devolve consumo de outros provedores na mesma
    etapa (ex.: fallback Helius/Shyft do roteador Solana).
    """
    remaining = remaining_today(db_path, provider)
    if remaining is not None and remaining < float(min_units):
        record_spend(db_path, provider, 0, chain=chain, stage=stage, skipped=True)
        return {
            "status": "COST_CAP_REACHED",
            "provider": str(provider).upper(),
            "daily_cap": daily_cap(provider),
            "spent_today": spent_today(db_path, provider),
        }
    result = fn(remaining)
    if isinstance(result, dict):
        record_spend(db_path, provider, units_from(result), chain=chain, stage=stage)
        if extra_spend is not None:
            for other, units in (extra_spend(result) or {}).items():
                if units:
                    record_spend(db_path, other, units, chain=chain, stage=stage)
    return result


def spend_report(db_path: Path, *, hours: int = 24, now_epoch: int | None = None) -> dict[str, dict]:
    now = int(time.time()) if now_epoch is None else int(now_epoch)
    cutoff = now - max(1, int(hours)) * 3600
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT provider, COALESCE(SUM(units), 0), COALESCE(SUM(skipped), 0)
            FROM provider_spend WHERE recorded_epoch >= ? GROUP BY provider
            """,
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    report: dict[str, dict] = {}
    for provider, units, skipped in rows:
        provider = str(provider)
        report[provider] = {
            "units": round(float(units or 0.0), 2),
            "unit": PROVIDER_UNITS.get(provider, "chamadas"),
            "skipped_by_cap": int(skipped or 0),
            "daily_cap": daily_cap(provider),
        }
    return report


# Extratores de consumo dos resumos que cada etapa já devolve.
def http_calls(result: dict) -> float:
    return _as_float(result.get("http_calls"))


def dune_executions(result: dict) -> float:
    return _as_float(result.get("dune_executions"))


def quicknode_credits(result: dict) -> float:
    return _as_float(result.get("quicknode_credits"))


def solana_fallback_calls(result: dict) -> dict[str, float]:
    calls = result.get("provider_calls") if isinstance(result.get("provider_calls"), dict) else {}
    return {"HELIUS": _as_float(calls.get("helius")), "SHYFT": _as_float(calls.get("shyft"))}


def rotating_nansen_calls(result: dict) -> float:
    return _as_float(result.get("nansen_http_calls"))


def rotating_zerion_calls(result: dict) -> dict[str, float]:
    return {"ZERION": _as_float(result.get("zerion_http_calls"))}


def budgeted_batch(db_path: Path, provider: str, configured: int, *, units_per_item: float = 1.0) -> int:
    """Tamanho de lote que cabe no que resta do teto diário do provedor."""
    configured = max(0, int(configured))
    remaining = remaining_today(db_path, provider)
    if remaining is None:
        return configured
    return max(0, min(configured, int(remaining // max(1e-9, float(units_per_item)))))


# Custo aproximado por wallet consultada (para dimensionar lotes).
UNITS_PER_WALLET = {"NANSEN": 2.0, "ZERION": 3.0, "COINSTATS": 3.0, "BIRDEYE": 2.0}
