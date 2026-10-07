from __future__ import annotations

from pathlib import Path
import json


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _pct(value) -> str:
    try:
        return f"{100.0 * float(value):.0f}%"
    except Exception:
        return "n/d"


def _cost(value) -> str:
    try:
        if value is None:
            return "n/d"
        return f"{float(value):.1f}"
    except Exception:
        return "n/d"


def enhance_status_text(base_text: str, data_dir: Path) -> str:
    payload = _load(data_dir / "state" / "enrichment_efficiency.json")
    if not payload:
        return base_text
    funnel = payload.get("funnel") if isinstance(payload.get("funnel"), dict) else {}
    reasons = payload.get("waiting_reasons") if isinstance(payload.get("waiting_reasons"), dict) else {}
    providers = payload.get("providers_24h") if isinstance(payload.get("providers_24h"), dict) else {}
    queue = payload.get("adaptive_queue") if isinstance(payload.get("adaptive_queue"), dict) else {}

    lines = [
        "",
        "📡 EFICIÊNCIA DE ENRIQUECIMENTO",
        f"✅ Com score: {int(funnel.get('scored', 0) or 0)} | ⏳ Esperando: {int(funnel.get('waiting', 0) or 0)}",
        (
            "🧩 Motivos: "
            f"sem WR {int(reasons.get('no_winrate', 0) or 0)} | "
            f"amostra {int(reasons.get('low_sample', 0) or 0)} | "
            f"PnL {int(reasons.get('missing_pnl', 0) or 0)} | "
            f"repetibilidade {int(reasons.get('missing_repeatability', 0) or 0)} | "
            f"outros {int(reasons.get('other', 0) or 0)}"
        ),
        f"🎛️ Fila adaptativa: {int(queue.get('due_wallets', 0) or 0)} prontas | {int(queue.get('monitor_suppressed', 0) or 0)} em monitoramento",
    ]

    labels = {"nansen": "Nansen", "zerion": "Zerion", "coinstats": "CoinStats"}
    for key in ("nansen", "zerion", "coinstats"):
        item = providers.get(key) if isinstance(providers.get(key), dict) else {}
        attempted = int(item.get("attempted", 0) or 0)
        if not attempted and not item.get("http_calls"):
            continue
        lines.append(
            f"🔌 {labels[key]} 24h: {int(item.get('enriched', 0) or 0)}/{attempted} enriquecidas | "
            f"sucesso {_pct(item.get('success_rate'))} | calls {int(item.get('http_calls', 0) or 0)} | "
            f"créditos/enriquecida {_cost(item.get('credits_per_enriched'))}"
        )

    lines.extend(_cost_lines(payload.get("cost") if isinstance(payload.get("cost"), dict) else {}))
    rows = payload.get("backtest_summary_rows") if isinstance(payload.get("backtest_summary_rows"), list) else []
    lines.extend(_backtest_lines(rows))
    lines.append("🧾 Evidência: chain+wallet + fonte + janela + método + divergência")
    return str(base_text).rstrip() + "\n" + "\n".join(lines)


_PROVIDER_LABELS = {
    "NANSEN": "Nansen", "BIRDEYE": "Birdeye", "ZERION": "Zerion", "COINSTATS": "CoinStats",
    "DUNE": "Dune", "QUICKNODE": "QuickNode", "HELIUS": "Helius", "SHYFT": "Shyft",
}


def _fmt_units(value) -> str:
    number = float(value or 0)
    if number >= 10_000:
        return f"{number / 1000:.0f}k"
    return f"{number:.0f}" if number >= 10 or number == int(number) else f"{number:.1f}"


def _cost_lines(cost: dict) -> list[str]:
    if not cost:
        return []
    lines = ["", f"💸 CUSTO (perfil {cost.get('mode', 'economy')})"]
    spend = cost.get("spend_24h") if isinstance(cost.get("spend_24h"), dict) else {}
    for provider in sorted(spend):
        item = spend[provider] if isinstance(spend[provider], dict) else {}
        cap = float(item.get("daily_cap") or 0)
        used = _fmt_units(item.get("units"))
        limit = f"/{_fmt_units(cap)}" if cap else ""
        alert = " ⛔ teto atingido" if int(item.get("skipped_by_cap") or 0) else ""
        lines.append(f"• {_PROVIDER_LABELS.get(provider, provider)} 24h: {used}{limit} {item.get('unit', '')}{alert}")
    rpc = cost.get("rpc_today") if isinstance(cost.get("rpc_today"), dict) else {}
    for provider in sorted(rpc):
        item = rpc[provider] if isinstance(rpc[provider], dict) else {}
        limit = int(item.get("limit") or 0)
        alert = " ⛔ teto atingido" if int(item.get("denied") or 0) else ""
        lines.append(f"• RPC {provider} hoje: {_fmt_units(item.get('used'))}{'/' + _fmt_units(limit) if limit else ''}{alert}")
    if len(lines) == 2:
        lines.append("• Nenhum gasto pago registrado nas últimas 24h")
    per_alpha = cost.get("cost_per_new_alpha_7d") if isinstance(cost.get("cost_per_new_alpha_7d"), dict) else {}
    new_alpha = int(cost.get("new_alpha_7d") or 0)
    if per_alpha:
        parts = [f"{_PROVIDER_LABELS.get(p, p)} {_fmt_units(v)}" for p, v in sorted(per_alpha.items())]
        lines.append(f"🎯 Custo por alpha nova (7d, n={new_alpha}): " + " · ".join(parts))
    else:
        lines.append(f"🎯 Custo por alpha nova (7d): sem alpha nova ainda (n={new_alpha})")
    return lines


def _backtest_lines(rows: list) -> list[str]:
    from .score_outcomes import track_record_text

    def clean(row: dict) -> dict:
        # NaN vindo do CSV/JSON vira ausência de dado, nunca "nan%".
        out = {}
        for key, value in row.items():
            try:
                out[key] = None if value is None or value != value else value
            except Exception:
                out[key] = value
        return out

    lines = ["", "🧪 BACKTEST 30d (fora da amostra)"]
    by_tier = {
        str(r.get("tier")): clean(r) for r in rows
        if isinstance(r, dict) and int(float(r.get("horizon_days") or 0)) == 30
    }
    if not by_tier:
        lines.append("Coletando sinais; os primeiros resultados saem 30 dias após o primeiro score.")
        return lines
    for tier in ("S", "A+", "A", "B"):
        lines.append(track_record_text(by_tier.get(tier), tier).replace("📈 Histórico do tier ", "• "))
    control = next((r for r in by_tier.values() if r.get("control_kept_wr60_rate") not in (None, "")), None)
    if control is not None:
        try:
            lines.append(f"• Controle (C/D): {100 * float(control['control_kept_wr60_rate']):.0f}% mantiveram WR≥60%")
        except (TypeError, ValueError):
            pass
    return lines
