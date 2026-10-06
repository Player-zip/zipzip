"""Diagnóstico das wallets paradas sem evidência e seleção para /acelerar.

Cada wallet sem win rate na tabela final cai em um motivo:

- ``nao_elegivel``: contrato/tipo de endereço que nunca terá win rate;
- ``sem_historico``: provedor respondeu, mas não há posições fechadas;
- ``erro_provedor``: a última consulta falhou;
- ``nunca_consultada``: nenhuma fonte de PnL tentou ainda.

Só ``nunca_consultada`` e ``erro_provedor`` melhoram com aceleração.
Nada aqui chama API: lê a tabela final, a fila e os caches dos provedores.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd

from .cost_control import UNITS_PER_WALLET, daily_cap, remaining_today
from .evidence_ledger import infer_chain, normalize_address, provider_health

CHAINS = ("solana", "base", "robinhood")
REASONS = ("nunca_consultada", "erro_provedor", "sem_historico", "nao_elegivel")
REASON_LABELS = {
    "nunca_consultada": "nunca consultadas",
    "erro_provedor": "erro no provedor",
    "sem_historico": "sem histórico no provedor",
    "nao_elegivel": "não elegíveis (contrato/tipo)",
}
CHAIN_LABELS = {"solana": "🟣 SOLANA", "base": "🔵 BASE", "robinhood": "🟢 ROBINHOOD"}
# Provedores que dão win rate para cada rede (na ordem em que são tentados).
CHAIN_PROVIDERS = {"solana": ("BIRDEYE",), "base": ("NANSEN", "ZERION"), "robinhood": ("NANSEN", "ZERION")}


def _read_csv(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path) if path.is_file() else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _present(value) -> bool:
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    return str(value).strip().lower() not in {"", "nan", "none", "null"}


def has_win_rate(row) -> bool:
    return _present(row.get("win_rate")) or _present(row.get("gmgn_winrate_30d"))


def is_ineligible(row) -> bool:
    address_type = str(row.get("address_type", "") or "").strip().upper()
    queue_status = str(row.get("queue_status", "") or "").strip().upper()
    if address_type == "CONTRACT" or queue_status in {"SKIP_CONTRACT", "REVIEW_ADDRESS_TYPE"}:
        return True
    eligible = row.get("eligible_enrichment")
    if _present(eligible) and str(eligible).strip().lower() in {"false", "0", "no"}:
        return True
    return False


def priority_of(row) -> float:
    for key in ("execution_priority_score", "discovery_score"):
        value = row.get(key)
        if _present(value):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return 0.0


def load_final_table(output_dir: Path) -> pd.DataFrame:
    live = output_dir / "V22S_wallet_stage1.csv"
    completed = output_dir / "V22S_wallet_stage1_completed.csv"
    frame = _read_csv(live)
    return frame if not frame.empty else _read_csv(completed)


def _provider_records(state_dir: Path, chain: str) -> dict[str, dict[str, dict]]:
    """address -> {"checked": {...}} / {"failed": {...}} por provedor da rede."""
    records: dict[str, dict[str, dict]] = {}

    def add(address: str, kind: str, payload: dict) -> None:
        records.setdefault(str(address), {})[kind] = payload

    if chain == "solana":
        cache = _load_json(state_dir / "birdeye_pnl_cache.json")
        for wallet, item in (cache.get("wallets") or {}).items():
            if not isinstance(item, dict):
                continue
            status = item.get("http_status")
            if status not in (None, 200):
                add(wallet, "failed", {"epoch": int(item.get("checked_epoch", 0) or 0), "status": status})
            else:
                add(wallet, "checked", {"epoch": int(item.get("checked_epoch", 0) or 0), "metrics": item.get("metrics") or {}})
        return records

    for provider in ("nansen", "zerion"):
        cache = _load_json(state_dir / f"{provider}_pnl_{chain}_cache.json")
        for address, item in (cache.get("entries") or {}).items():
            if isinstance(item, dict) and isinstance(item.get("metrics"), dict):
                previous = records.get(str(address).lower(), {}).get("checked")
                if not previous or _present(item["metrics"].get("win_rate")):
                    add(str(address).lower(), "checked", {"epoch": int(item.get("checked_epoch", 0) or 0), "metrics": item["metrics"]})
        for address, item in (cache.get("failures") or {}).items():
            if isinstance(item, dict):
                add(str(address).lower(), "failed", {"epoch": int(item.get("failed_epoch", 0) or 0), "statuses": item.get("statuses")})
    if chain == "robinhood":
        rotation = _load_json(state_dir / "provider_legacy_robinhood_rotation.json")
        for group in ("nansen_attempts", "zerion_attempts"):
            for address, attempt in (rotation.get(group) or {}).items():
                if isinstance(attempt, dict) and attempt.get("status") == "ERROR" and "checked" not in records.get(str(address).lower(), {}):
                    add(str(address).lower(), "failed", {"epoch": int(attempt.get("last_attempt_epoch", 0) or 0), "statuses": attempt.get("http_statuses")})
    return records


def classify_waiting(cfg) -> pd.DataFrame:
    """Wallets da tabela final sem win rate, com motivo e prioridade."""
    final = load_final_table(cfg.output_dir)
    if final.empty or "address" not in final.columns:
        return pd.DataFrame(columns=["wallet_key", "chain", "address", "reason", "priority"])
    records = {chain: _provider_records(cfg.state_dir, chain) for chain in CHAINS}
    rows = []
    for row in final.to_dict("records"):
        if has_win_rate(row):
            continue
        chain = infer_chain(row)
        address = normalize_address(chain, str(row.get("address") or "").strip())
        if not address:
            continue
        if is_ineligible(row):
            reason = "nao_elegivel"
        else:
            info = records.get(chain, {}).get(address, {})
            checked = info.get("checked")
            failed = info.get("failed")
            if failed and (not checked or failed.get("epoch", 0) >= checked.get("epoch", 0)):
                reason = "erro_provedor"
            elif checked:
                reason = "sem_historico"
            else:
                reason = "nunca_consultada"
        rows.append({
            "wallet_key": f"{chain}:{address}",
            "chain": chain,
            "address": address,
            "reason": reason,
            "priority": priority_of(row),
        })
    return pd.DataFrame(rows)


def chain_counts(cfg) -> dict[str, dict]:
    """Totais por rede: wallets, com score, aguardando (por motivo)."""
    final = load_final_table(cfg.output_dir)
    waiting = classify_waiting(cfg)
    out: dict[str, dict] = {}
    for chain in CHAINS:
        subset = final[[infer_chain(r) == chain for r in final.to_dict("records")]] if not final.empty else final
        scores = pd.to_numeric(subset.get("selective_alpha_score", pd.Series(dtype=float)), errors="coerce") if not subset.empty else pd.Series(dtype=float)
        waits = waiting[waiting["chain"].eq(chain)] if not waiting.empty else waiting
        out[chain] = {
            "wallets": int(len(subset)),
            "scored": int(scores.notna().sum()),
            "waiting": int(len(waits)),
            "reasons": {reason: int(waits["reason"].eq(reason).sum()) if not waits.empty else 0 for reason in REASONS},
        }
    return out


def _fmt_time(epoch: int) -> str:
    return time.strftime("%d/%m %H:%M UTC", time.gmtime(int(epoch)))


def provider_status(cfg, chain: str) -> list[str]:
    """Saúde e orçamento das fontes de win rate da rede."""
    lines = []
    now = int(time.time())
    for provider in CHAIN_PROVIDERS.get(chain, ()):
        key = {"NANSEN": cfg.nansen_api_key, "ZERION": cfg.zerion_api_key, "BIRDEYE": cfg.birdeye_api_key}.get(provider)
        name = provider.title()
        if not key:
            lines.append(f"• {name}: ⚪ sem chave configurada")
            continue
        parts = []
        if provider == "NANSEN":
            state = _load_json(cfg.state_dir / f"nansen_provider_{chain}_state.json")
            until = int(state.get("cooldown_until", 0) or 0)
            if until > now:
                parts.append(f"⛔ pausado até {_fmt_time(until)} (último HTTP {state.get('last_status')})")
            health = provider_health(cfg.master_db, "NANSEN", chain, hours=24)
            if health.get("attempted"):
                parts.append(f"24h: {health.get('enriched', 0)}/{health.get('attempted', 0)} com sucesso")
        remaining = remaining_today(cfg.master_db, provider)
        per_wallet = UNITS_PER_WALLET.get(provider, 2.0)
        if remaining is None:
            parts.append("sem teto diário")
        else:
            parts.append(f"orçamento hoje: ~{int(remaining // per_wallet)} wallets ({int(remaining)}/{int(daily_cap(provider))})")
        if not any(p.startswith("⛔") for p in parts):
            parts.insert(0, "✅ ativo")
        lines.append(f"• {name}: " + " · ".join(parts))
    return lines


def diagnosis_text(cfg) -> str:
    counts = chain_counts(cfg)
    lines = ["🧯 GARGALO — wallets sem evidência", ""]
    total_actionable = 0
    for chain in CHAINS:
        item = counts[chain]
        if not cfg.chain_enabled(chain):
            lines.append(f"{CHAIN_LABELS[chain]}: desligada em PEIXAO_CHAINS")
            lines.append("")
            continue
        reasons = item["reasons"]
        actionable = reasons["nunca_consultada"] + reasons["erro_provedor"]
        total_actionable += actionable
        lines.append(f"{CHAIN_LABELS[chain]}: {item['waiting']} aguardando de {item['wallets']} ({item['scored']} com score)")
        for reason in REASONS:
            if reasons[reason]:
                lines.append(f"  · {reasons[reason]} {REASON_LABELS[reason]}")
        lines.extend("  " + line for line in provider_status(cfg, chain))
        if actionable:
            lines.append(f"  ➜ {actionable} podem andar com /acelerar {chain} <quantidade>")
        lines.append("")
    if counts and sum(c["reasons"]["nao_elegivel"] for c in counts.values()):
        lines.append("Não elegíveis nunca terão win rate: não gastam orçamento.")
    if sum(c["reasons"]["sem_historico"] for c in counts.values()):
        lines.append("Sem histórico: o provedor respondeu sem posições fechadas; só são reconsultadas após o prazo de PEIXAO_NO_DATA_RETRY_SECONDS.")
    if not total_actionable:
        lines.append("Nada acelerável agora.")
    return "\n".join(lines).rstrip()


def select_for_boost(cfg, chain: str, quantity: int) -> list[dict]:
    """Wallets que valem gastar: nunca consultadas primeiro, depois erros; por prioridade."""
    waiting = classify_waiting(cfg)
    if waiting.empty:
        return []
    subset = waiting[waiting["chain"].eq(chain) & waiting["reason"].isin(["nunca_consultada", "erro_provedor"])].copy()
    if subset.empty:
        return []
    subset["_reason_rank"] = subset["reason"].map({"nunca_consultada": 0, "erro_provedor": 1})
    subset = subset.sort_values(["_reason_rank", "priority"], ascending=[True, False], kind="mergesort")
    return subset.head(max(0, int(quantity))).drop(columns=["_reason_rank"]).to_dict("records")
