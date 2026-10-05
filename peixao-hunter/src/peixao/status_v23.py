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

    backtest = payload.get("backtest") if isinstance(payload.get("backtest"), dict) else {}
    rows = payload.get("backtest_summary_rows") if isinstance(payload.get("backtest_summary_rows"), list) else []
    if rows:
        lines.append(f"🧪 Backtest: {int(backtest.get('summary_rows', len(rows)) or len(rows))} grupos 7/14/30d")
    else:
        lines.append("🧪 Backtest: coletando histórico para 7/14/30d")
    lines.append("🧾 Evidência: chain+wallet + fonte + janela + método + divergência")
    return str(base_text).rstrip() + "\n" + "\n".join(lines)
