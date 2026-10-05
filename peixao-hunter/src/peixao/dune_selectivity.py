from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import os
import re
import time

import pandas as pd
import requests


SOLANA_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
EXCLUDED_MINTS = (
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _safe_wallet(value: str) -> str | None:
    value = str(value or "").strip()
    return value if SOLANA_RE.fullmatch(value) else None


def _sql(wallets: list[str], days: int) -> str:
    values = ",\n        ".join("('" + w + "')" for w in wallets)
    excluded = ", ".join("'" + mint + "'" for mint in EXCLUDED_MINTS)
    days = max(7, min(90, int(days)))
    return f"""
WITH wallets(wallet) AS (
    VALUES {values}
), raw AS (
    SELECT trader_id AS wallet, block_time, token_bought_mint_address, token_sold_mint_address, amount_usd
    FROM dex_solana.trades
    WHERE block_date >= CURRENT_DATE - INTERVAL '{days}' DAY
      AND trader_id IN (SELECT wallet FROM wallets)
      AND amount_usd IS NOT NULL
      AND amount_usd > 0
), events AS (
    SELECT wallet, block_time, token_bought_mint_address AS token, amount_usd, 'buy' AS side
    FROM raw
    WHERE token_bought_mint_address IS NOT NULL
      AND token_bought_mint_address NOT IN ({excluded})
    UNION ALL
    SELECT wallet, block_time, token_sold_mint_address AS token, amount_usd, 'sell' AS side
    FROM raw
    WHERE token_sold_mint_address IS NOT NULL
      AND token_sold_mint_address NOT IN ({excluded})
), token_flows AS (
    SELECT wallet, token,
           SUM(CASE WHEN side = 'buy' THEN amount_usd ELSE 0 END) AS buy_usd,
           SUM(CASE WHEN side = 'sell' THEN amount_usd ELSE 0 END) AS sell_usd,
           MIN(CASE WHEN side = 'buy' THEN block_time END) AS first_buy
    FROM events
    GROUP BY 1,2
), closed AS (
    SELECT wallet, token, sell_usd - buy_usd AS pnl_usd
    FROM token_flows
    WHERE buy_usd > 0 AND sell_usd > 0
), closed_stats AS (
    SELECT wallet,
           COUNT(*) AS closed_positions,
           COUNT_IF(pnl_usd > 0) AS winning_positions,
           SUM(pnl_usd) AS realized_pnl_proxy,
           APPROX_PERCENTILE(pnl_usd, 0.5) AS median_pnl_per_position
    FROM closed
    GROUP BY 1
), weekly_entries AS (
    SELECT wallet, DATE_TRUNC('week', first_buy) AS week, COUNT(*) AS new_positions
    FROM token_flows
    WHERE first_buy IS NOT NULL
    GROUP BY 1,2
), entry_stats AS (
    SELECT wallet, AVG(CAST(new_positions AS DOUBLE)) AS new_positions_per_week,
           MAX(new_positions) AS max_new_positions_week
    FROM weekly_entries
    GROUP BY 1
), weekly_net AS (
    SELECT wallet, DATE_TRUNC('week', block_time) AS week,
           SUM(CASE WHEN side = 'sell' THEN amount_usd ELSE -amount_usd END) AS net_usd
    FROM events
    GROUP BY 1,2
), weekly_stats AS (
    SELECT wallet, COUNT(*) AS active_weeks,
           COUNT_IF(net_usd > 0) AS positive_active_weeks
    FROM weekly_net
    GROUP BY 1
)
SELECT w.wallet,
       e.new_positions_per_week AS dune_new_positions_per_week,
       e.max_new_positions_week AS dune_max_new_positions_week,
       c.closed_positions AS dune_closed_positions_30d,
       c.winning_positions AS dune_winning_positions_30d,
       CASE WHEN c.closed_positions > 0 THEN CAST(c.winning_positions AS DOUBLE) / c.closed_positions END AS dune_win_rate_30d,
       c.realized_pnl_proxy AS dune_realized_pnl_proxy_30d,
       c.median_pnl_per_position AS dune_median_pnl_per_position,
       s.active_weeks AS dune_active_weeks,
       s.positive_active_weeks AS dune_positive_active_weeks,
       CASE WHEN s.active_weeks > 0 THEN CAST(s.positive_active_weeks AS DOUBLE) / s.active_weeks END AS dune_repeatability_score
FROM wallets w
LEFT JOIN entry_stats e ON e.wallet = w.wallet
LEFT JOIN closed_stats c ON c.wallet = w.wallet
LEFT JOIN weekly_stats s ON s.wallet = w.wallet
""".strip()


def _rows(payload: dict) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    result = payload.get("result")
    if isinstance(result, dict) and isinstance(result.get("rows"), list):
        return [x for x in result["rows"] if isinstance(x, dict)]
    return []


def enrich_stage1_with_dune(
    stage1_path: Path,
    output_dir: Path,
    state_dir: Path,
    *,
    api_key: str | None,
    enabled: bool = True,
    base_url: str = "https://api.dune.com/api/v1",
    timeout: float = 20.0,
    poll_interval: float = 2.0,
    max_poll_seconds: float = 60.0,
    ttl_seconds: int = 86400,
    max_wallets: int = 20,
    lookback_days: int = 30,
) -> dict:
    """Validate only the best Solana Stage-1 candidates with one batched DuneSQL query.

    This stage never replaces Birdeye PnL. Its 30-day trade-flow metrics are kept
    under dune_* names and used as independent evidence / selectivity validation.
    At most one paid Dune execution is started per run.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "V22_wallet_dune_enriched.csv"
    if not stage1_path.is_file():
        return {"status": "SKIPPED_NO_STAGE1", "http_calls": 0, "dune_executions": 0, "wallets_requested": 0}
    try:
        frame = pd.read_csv(stage1_path)
    except pd.errors.EmptyDataError:
        frame = pd.DataFrame()
    if frame.empty or "address" not in frame.columns:
        frame.to_csv(out_path, index=False)
        return {"status": "DONE_EMPTY", "http_calls": 0, "dune_executions": 0, "wallets_requested": 0, "output": str(out_path)}

    # Strip the pre-Dune alpha outputs before the final re-score. Raw evidence is kept.
    raw_cols = [c for c in frame.columns if not c.startswith("alpha22_")]
    base = frame[raw_cols].copy()
    ranked = frame.copy()
    if "alpha22_score" in ranked.columns:
        ranked = ranked.sort_values(["alpha22_score", "discovery_score" if "discovery_score" in ranked.columns else "address"], ascending=[False, False], na_position="last")
    wallets = []
    for value in ranked["address"].astype(str):
        wallet = _safe_wallet(value)
        if wallet and wallet not in wallets:
            wallets.append(wallet)
        if len(wallets) >= max(0, int(max_wallets)):
            break

    key = str(api_key or "").strip()
    if not enabled:
        base.to_csv(out_path, index=False)
        return {"status": "DISABLED", "http_calls": 0, "dune_executions": 0, "wallets_requested": len(wallets), "output": str(out_path)}
    if not key:
        base.to_csv(out_path, index=False)
        return {"status": "NOT_CONFIGURED", "http_calls": 0, "dune_executions": 0, "wallets_requested": len(wallets), "output": str(out_path)}
    if not wallets:
        base.to_csv(out_path, index=False)
        return {"status": "DONE_NO_SOLANA_WALLETS", "http_calls": 0, "dune_executions": 0, "wallets_requested": 0, "output": str(out_path)}

    cache_path = state_dir / "dune_selectivity_cache.json"
    cache = _load_json(cache_path)
    entries = cache.get("wallets") if isinstance(cache.get("wallets"), dict) else {}
    now = int(time.time())
    stale = [w for w in wallets if not isinstance(entries.get(w), dict) or now - int(entries[w].get("checked_epoch", 0) or 0) >= max(0, int(ttl_seconds))]
    http_calls = 0
    executions = 0
    execution_id = None
    query_status = None

    if stale:
        headers = {"X-DUNE-API-KEY": key, "Content-Type": "application/json", "Accept": "application/json"}
        try:
            response = requests.post(
                base_url.rstrip("/") + "/sql/execute",
                headers=headers,
                json={"sql": _sql(stale, lookback_days), "performance": "medium"},
                timeout=float(timeout),
            )
            http_calls += 1
            if 200 <= response.status_code < 300:
                payload = response.json()
                execution_id = payload.get("execution_id") if isinstance(payload, dict) else None
                executions = 1 if execution_id else 0
                query_status = "SUBMITTED" if execution_id else "NO_EXECUTION_ID"
            elif response.status_code in (401, 403):
                query_status = "AUTH_ERROR"
            elif response.status_code == 402:
                query_status = "PAYMENT_REQUIRED"
            elif response.status_code == 429:
                query_status = "RATE_LIMITED"
            else:
                query_status = f"HTTP_{response.status_code}"
        except requests.RequestException:
            query_status = "NETWORK_ERROR"

        if execution_id:
            deadline = time.time() + max(1.0, float(max_poll_seconds))
            while time.time() < deadline:
                try:
                    result = requests.get(
                        base_url.rstrip("/") + f"/execution/{execution_id}/results",
                        headers={"X-DUNE-API-KEY": key, "Accept": "application/json"},
                        timeout=float(timeout),
                    )
                    http_calls += 1
                    if result.status_code == 200:
                        payload = result.json()
                        state = str(payload.get("state", "")) if isinstance(payload, dict) else ""
                        if state == "QUERY_STATE_COMPLETED":
                            query_status = "DONE"
                            returned = {str(r.get("wallet", "")): r for r in _rows(payload) if r.get("wallet")}
                            for wallet in stale:
                                entries[wallet] = {
                                    "checked_epoch": now,
                                    "checked_at": _now_iso(),
                                    "metrics": returned.get(wallet, {}),
                                }
                            break
                        if state in {"QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"}:
                            query_status = state
                            break
                    elif result.status_code == 429:
                        query_status = "RATE_LIMITED"
                        break
                except requests.RequestException:
                    query_status = "NETWORK_ERROR"
                    break
                time.sleep(max(0.25, float(poll_interval)))
            else:
                query_status = "TIMEOUT"

    _atomic_json(cache_path, {"wallets": entries})
    metrics_rows = []
    for wallet in wallets:
        entry = entries.get(wallet) if isinstance(entries.get(wallet), dict) else {}
        metrics = entry.get("metrics") if isinstance(entry.get("metrics"), dict) else {}
        if metrics:
            metrics_rows.append({"address": wallet, **metrics, "dune_selectivity_source": "dex_solana.trades_30d_tradeflow_proxy"})
    metrics_frame = pd.DataFrame(metrics_rows)
    if not metrics_frame.empty:
        base = base.merge(metrics_frame, on="address", how="left")
    base.to_csv(out_path, index=False)
    return {
        "status": "DONE" if query_status in (None, "DONE") else str(query_status),
        "http_calls": int(http_calls),
        "dune_executions": int(executions),
        "wallets_requested": int(len(wallets)),
        "wallets_refreshed": int(len(stale) if query_status == "DONE" else 0),
        "wallets_with_metrics": int(len(metrics_frame)),
        "execution_id": execution_id,
        "output": str(out_path),
    }


def load_dune_metrics(state_dir: Path, *, max_age_seconds: int = 7 * 86400) -> dict[str, dict]:
    """Métricas Dune em cache por wallet Solana, para o construtor final.

    A validação Dune roda no ciclo de 6h; o ciclo horário reaproveita o cache
    enquanto ele estiver dentro da idade máxima.
    """
    cache = _load_json(state_dir / "dune_selectivity_cache.json")
    entries = cache.get("wallets") if isinstance(cache.get("wallets"), dict) else {}
    now = int(time.time())
    out: dict[str, dict] = {}
    for wallet, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        metrics = entry.get("metrics") if isinstance(entry.get("metrics"), dict) else {}
        if not metrics:
            continue
        if max_age_seconds > 0 and now - int(entry.get("checked_epoch", 0) or 0) > max_age_seconds:
            continue
        clean = {k: v for k, v in metrics.items() if str(k).startswith("dune_")}
        if clean:
            clean["dune_selectivity_source"] = "dex_solana.trades_30d_tradeflow_proxy"
            out[str(wallet)] = clean
    return out
