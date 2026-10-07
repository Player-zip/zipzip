"""Backtest honesto do score, sem nenhuma chamada extra a API.

1. Sinal: a primeira vez que uma wallet atinge um tier (S, A+, A, B, C, D)
   vira um registro imutável com a foto do momento (T0). Os tiers C/D são o
   grupo de controle.
2. Resultado: em T0+7/14/30 dias procura no ledger a primeira observação
   real de provedor (data de consulta do cache: Nansen, Zerion, CoinStats,
   Birdeye) depois do horizonte. Linhas copiadas do pipeline não contam,
   porque não têm data real de consulta.
3. Sem viés de sobrevivência: todo sinal é avaliado, esteja a wallet na tabela
   ou não. Sem observação até o fim da janela vira ``NO_OBSERVATION`` e entra
   na cobertura.

No horizonte de 30 dias a janela "30d" do provedor cobre só o período depois
do sinal, ou seja, é fora da amostra. Em 7/14 dias há sobreposição com o
período anterior ao sinal (marcado em ``out_of_sample``).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import time

import pandas as pd

from .evidence_ledger import ROW_METHODOLOGY, _PROVIDER_PRIORITY, infer_chain, normalize_address, wallet_key
from .evidence_ledger import _connect as _ledger_connect

HORIZONS = (7, 14, 30)
GRACE_DAYS = 7
TIER_ORDER = ("S", "A+", "A", "B", "C", "D")
CONTROL_TIERS = ("C", "D")
WIN_RATE_GATE = 0.60
MIN_OBSERVED_FOR_STATS = 5
SUMMARY_FILE = "V23_score_outcomes_summary.csv"


def _now_iso(epoch: int | None = None) -> str:
    moment = datetime.fromtimestamp(int(time.time()) if epoch is None else int(epoch), timezone.utc)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _num(value):
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _connect(db_path: Path) -> sqlite3.Connection:
    _ledger_connect(db_path).close()  # garante wallet_evidence
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS score_signals (
            signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet_key TEXT NOT NULL,
            chain TEXT NOT NULL,
            address TEXT NOT NULL,
            score_version TEXT NOT NULL,
            tier TEXT NOT NULL,
            score REAL,
            deep_dive INTEGER NOT NULL DEFAULT 0,
            signal_epoch INTEGER NOT NULL,
            signal_at TEXT NOT NULL,
            win_rate_t0 REAL,
            profit_t0 REAL,
            roi_t0 REAL,
            closed_t0 REAL,
            UNIQUE(wallet_key, score_version, tier)
        );
        CREATE INDEX IF NOT EXISTS idx_score_signals_epoch ON score_signals(signal_epoch);
        CREATE TABLE IF NOT EXISTS signal_outcomes (
            signal_id INTEGER NOT NULL,
            horizon_days INTEGER NOT NULL,
            status TEXT NOT NULL,
            evaluated_at TEXT NOT NULL,
            observed_epoch INTEGER,
            provider TEXT,
            win_rate_after REAL,
            profit_after REAL,
            roi_after REAL,
            out_of_sample INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (signal_id, horizon_days)
        );
        """
    )
    conn.commit()
    return conn


def register_signals(conn: sqlite3.Connection, stage: pd.DataFrame, *, now_epoch: int) -> int:
    if stage.empty or "address" not in stage.columns or "selective_alpha_tier" not in stage.columns:
        return 0
    added = 0
    for row in stage.to_dict("records"):
        tier = str(row.get("selective_alpha_tier") or "").strip()
        if tier not in TIER_ORDER:
            continue
        chain = infer_chain(row)
        address = normalize_address(chain, str(row.get("address") or "").strip())
        if not address:
            continue
        deep = str(row.get("selective_deep_dive_candidate", "")).strip().lower() in {"true", "1", "yes"}
        before = conn.total_changes
        conn.execute(
            """
            INSERT OR IGNORE INTO score_signals
            (wallet_key, chain, address, score_version, tier, score, deep_dive, signal_epoch, signal_at,
             win_rate_t0, profit_t0, roi_t0, closed_t0)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                wallet_key(chain, address), chain, address,
                str(row.get("selective_score_version") or "V2.2S1"), tier,
                _num(row.get("selective_alpha_score")), int(deep), int(now_epoch), _now_iso(now_epoch),
                _num(row.get("win_rate")), _num(row.get("realized_profit_30d")),
                _num(row.get("realized_roi_30d")), _num(row.get("closed_positions")),
            ),
        )
        added += int(conn.total_changes > before)
    return added


def _observation_after(conn: sqlite3.Connection, key: str, start: int, end: int) -> dict | None:
    rows = conn.execute(
        """
        SELECT provider, metric, value, observed_epoch FROM wallet_evidence
        WHERE wallet_key=? AND methodology != ? AND observed_epoch >= ? AND observed_epoch <= ?
          AND metric IN ('win_rate', 'realized_profit_30d', 'realized_roi_30d')
        ORDER BY observed_epoch ASC
        """,
        (key, ROW_METHODOLOGY, int(start), int(end)),
    ).fetchall()
    if not rows:
        return None
    by_provider: dict[str, dict] = {}
    for provider, metric, value, epoch in rows:
        item = by_provider.setdefault(str(provider), {"provider": str(provider), "observed_epoch": int(epoch)})
        item.setdefault(str(metric), float(value))
        item["observed_epoch"] = min(item["observed_epoch"], int(epoch))
    # Provedor de maior prioridade que trouxe win rate; senão o de maior prioridade.
    ranked = sorted(
        by_provider.values(),
        key=lambda item: ("win_rate" in item, _PROVIDER_PRIORITY.get(item["provider"], 0)),
        reverse=True,
    )
    return ranked[0]


def evaluate_outcomes(conn: sqlite3.Connection, *, now_epoch: int) -> dict:
    observed = missing = 0
    done = {
        (int(r[0]), int(r[1]))
        for r in conn.execute("SELECT signal_id, horizon_days FROM signal_outcomes").fetchall()
    }
    signals = conn.execute("SELECT signal_id, wallet_key, signal_epoch FROM score_signals").fetchall()
    for signal_id, key, signal_epoch in signals:
        for horizon in HORIZONS:
            if (int(signal_id), horizon) in done:
                continue
            start = int(signal_epoch) + horizon * 86400
            if now_epoch < start:
                continue
            end = start + GRACE_DAYS * 86400
            found = _observation_after(conn, str(key), start, end)
            if found is None and now_epoch <= end:
                continue  # ainda dentro da janela; tenta de novo no próximo ciclo
            status = "OBSERVED" if found else "NO_OBSERVATION"
            conn.execute(
                """
                INSERT OR IGNORE INTO signal_outcomes
                (signal_id, horizon_days, status, evaluated_at, observed_epoch, provider,
                 win_rate_after, profit_after, roi_after, out_of_sample)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(signal_id), horizon, status, _now_iso(now_epoch),
                    (found or {}).get("observed_epoch"), (found or {}).get("provider"),
                    (found or {}).get("win_rate"), (found or {}).get("realized_profit_30d"),
                    (found or {}).get("realized_roi_30d"), int(horizon >= 30),
                ),
            )
            observed += int(found is not None)
            missing += int(found is None)
    return {"observed": observed, "no_observation": missing}


def _rate(series: pd.Series) -> float | None:
    clean = series.dropna()
    return None if clean.empty else round(float(clean.mean()), 4)


def summarize(conn: sqlite3.Connection, *, now_epoch: int) -> pd.DataFrame:
    signals = pd.read_sql_query("SELECT signal_id, tier, signal_epoch FROM score_signals", conn)
    outcomes = pd.read_sql_query(
        "SELECT signal_id, horizon_days, status, win_rate_after, profit_after FROM signal_outcomes", conn,
    )
    rows: list[dict] = []
    if signals.empty:
        return pd.DataFrame()
    merged = outcomes.merge(signals, on="signal_id", how="left") if not outcomes.empty else pd.DataFrame()
    for horizon in HORIZONS:
        matured_cut = now_epoch - (horizon + GRACE_DAYS) * 86400
        horizon_rows = merged[merged["horizon_days"].eq(horizon)] if not merged.empty else pd.DataFrame()
        control = horizon_rows[horizon_rows["tier"].isin(CONTROL_TIERS) & horizon_rows["status"].eq("OBSERVED")] if not horizon_rows.empty else pd.DataFrame()
        control_kept = _rate(control["win_rate_after"].ge(WIN_RATE_GATE).where(control["win_rate_after"].notna())) if not control.empty else None
        for tier in TIER_ORDER:
            tier_signals = signals[signals["tier"].eq(tier)]
            tier_rows = horizon_rows[horizon_rows["tier"].eq(tier)] if not horizon_rows.empty else pd.DataFrame()
            observed = tier_rows[tier_rows["status"].eq("OBSERVED")] if not tier_rows.empty else pd.DataFrame()
            matured = int(len(tier_rows)) if not tier_rows.empty else 0
            wr = observed["win_rate_after"] if not observed.empty else pd.Series(dtype=float)
            profit = observed["profit_after"] if not observed.empty else pd.Series(dtype=float)
            kept = _rate(wr.ge(WIN_RATE_GATE).where(wr.notna())) if not wr.empty else None
            positive = _rate(profit.gt(0).where(profit.notna())) if not profit.empty else None
            rows.append({
                "horizon_days": horizon,
                "tier": tier,
                "signals": int(len(tier_signals)),
                "signals_old_enough": int(tier_signals["signal_epoch"].le(matured_cut).sum()),
                "evaluated": matured,
                "observed": int(len(observed)),
                "coverage": None if not matured else round(len(observed) / matured, 4),
                "kept_wr60_rate": kept,
                "positive_profit_rate": positive,
                "median_profit_after": None if profit.dropna().empty else round(float(profit.dropna().median()), 2),
                "control_kept_wr60_rate": control_kept,
                "lift_vs_control": None if kept is None or not control_kept else round(kept / control_kept, 3),
                "out_of_sample": horizon >= 30,
            })
    return pd.DataFrame(rows)


def update_score_outcomes(stage_path: Path, db_path: Path, output_path: Path, *, now_epoch: int | None = None) -> dict:
    now = int(time.time()) if now_epoch is None else int(now_epoch)
    try:
        stage = pd.read_csv(stage_path) if stage_path.is_file() else pd.DataFrame()
    except Exception:
        stage = pd.DataFrame()
    conn = _connect(db_path)
    try:
        added = register_signals(conn, stage, now_epoch=now)
        evaluated = evaluate_outcomes(conn, now_epoch=now)
        conn.commit()
        summary = summarize(conn, now_epoch=now)
    finally:
        conn.close()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    summary.to_csv(tmp, index=False)
    tmp.replace(output_path)
    return {
        "status": "DONE",
        "signals_added": int(added),
        **evaluated,
        "summary_rows": int(len(summary)),
        "output": str(output_path),
    }


def tier_track_record(output_dir: Path, tier: str, *, horizon: int = 30) -> dict | None:
    """Histórico de um tier para alertas e /status (None se não há resumo)."""
    path = output_dir / SUMMARY_FILE
    try:
        frame = pd.read_csv(path) if path.is_file() else pd.DataFrame()
    except Exception:
        return None
    if frame.empty:
        return None
    match = frame[(frame["horizon_days"] == horizon) & (frame["tier"].astype(str) == str(tier))]
    if match.empty:
        return None
    row = match.iloc[0].to_dict()
    return {k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in row.items()}


def track_record_text(record: dict | None, tier: str, *, horizon: int = 30) -> str:
    if not record or int(record.get("observed") or 0) < MIN_OBSERVED_FOR_STATS:
        observed = int((record or {}).get("observed") or 0)
        return f"📈 Histórico do tier {tier} ({horizon}d): coletando dados (n={observed})"
    kept = record.get("kept_wr60_rate")
    coverage = record.get("coverage")
    lift = record.get("lift_vs_control")
    parts = [
        f"📈 Histórico do tier {tier} ({horizon}d): "
        f"{100 * float(kept):.0f}% mantiveram WR≥60%" if kept is not None else f"📈 Histórico do tier {tier} ({horizon}d)",
        f"n={int(record.get('observed') or 0)}",
    ]
    if coverage is not None:
        parts.append(f"cobertura {100 * float(coverage):.0f}%")
    if lift is not None:
        parts.append(f"{float(lift):.1f}× o controle")
    return " · ".join(parts)
