from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import math
import sqlite3
import time

import pandas as pd

from .units import RATIO, ROI_UNIT_KEY, ratio_from_any, strict_ratio


# Métricas "30d" mais velhas que isto só são usadas se não houver nada recente.
DEFAULT_EVIDENCE_MAX_AGE_DAYS = 30
# Observações antigas (exceto a última de cada provedor/métrica) são apagadas.
DEFAULT_EVIDENCE_RETENTION_DAYS = 120
# Provedores cujos caches são sincronizados com data e atribuição corretas.
CACHE_SYNCED_PROVIDERS = {"NANSEN", "ZERION", "COINSTATS", "BIRDEYE"}
# Metodologia das linhas copiadas do pipeline (sem data real de consulta).
ROW_METHODOLOGY = "pipeline source row"
EVM_CHAINS = {"base", "robinhood", "ethereum", "evm"}

_PROVIDER_PRIORITY = {
    "NANSEN": 100,
    "ZERION": 90,
    "COINSTATS": 85,
    "BIRDEYE": 80,
    "GMGN_CACHE": 75,
    "INLINE": 50,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _float(value):
    try:
        if value is None or pd.isna(value):
            return None
        value = float(value)
        return value if math.isfinite(value) else None
    except Exception:
        return None


def normalize_chain(chain: str | None) -> str:
    value = str(chain or "").strip().lower()
    aliases = {"rh": "robinhood", "robinhood chain": "robinhood", "sol": "solana"}
    return aliases.get(value, value or "unknown")


def normalize_address(chain: str, address: str | None) -> str:
    value = str(address or "").strip()
    if normalize_chain(chain) in EVM_CHAINS:
        return value.lower()
    return value


_SOURCE_CHAINS = {
    "legacy_v6": "robinhood",
    "radar_robinhood": "robinhood",
    "radar_base": "base",
    "radar_solana": "solana",
}


def infer_chain(row, default: str | None = None) -> str:
    """Rede de uma linha do pipeline, com a mesma regra em todo lugar.

    Usa a coluna ``chain``; sem ela, a origem da linha; sem origem, o formato do
    endereço (``0x`` = EVM sem rede conhecida, o resto = Solana).
    """
    raw = row.get("chain") if hasattr(row, "get") else None
    text = "" if raw is None else str(raw).strip().lower()
    if text and text not in {"nan", "none", "null", "unknown"}:
        return normalize_chain(text)
    source = str(row.get("selective_input_source", "") or "").strip().lower() if hasattr(row, "get") else ""
    if source in _SOURCE_CHAINS:
        return _SOURCE_CHAINS[source]
    if default:
        return normalize_chain(default)
    address = str(row.get("address", "") or "").strip() if hasattr(row, "get") else ""
    if address.lower().startswith("0x") and len(address) == 42:
        return "evm"
    return "solana"


def wallet_key(chain: str, address: str) -> str:
    c = normalize_chain(chain)
    return f"{c}:{normalize_address(c, address)}"


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=20)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=20000")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS wallet_evidence (
            wallet_key TEXT NOT NULL,
            chain TEXT NOT NULL,
            address TEXT NOT NULL,
            provider TEXT NOT NULL,
            metric TEXT NOT NULL,
            value REAL,
            unit TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            observed_epoch INTEGER NOT NULL,
            window_days INTEGER,
            methodology TEXT,
            source_quality REAL NOT NULL DEFAULT 1.0,
            normalization_issue TEXT,
            PRIMARY KEY (wallet_key, provider, metric, observed_at)
        );
        CREATE INDEX IF NOT EXISTS idx_wallet_evidence_lookup
            ON wallet_evidence(wallet_key, metric, observed_epoch DESC);
        CREATE INDEX IF NOT EXISTS idx_wallet_evidence_latest
            ON wallet_evidence(wallet_key, provider, metric, observed_epoch DESC);

        CREATE TABLE IF NOT EXISTS provider_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at TEXT NOT NULL,
            recorded_epoch INTEGER NOT NULL,
            provider TEXT NOT NULL,
            chain TEXT NOT NULL,
            attempted INTEGER NOT NULL DEFAULT 0,
            enriched INTEGER NOT NULL DEFAULT 0,
            errors INTEGER NOT NULL DEFAULT 0,
            http_calls INTEGER NOT NULL DEFAULT 0,
            credits REAL NOT NULL DEFAULT 0,
            statuses TEXT NOT NULL DEFAULT '[]'
        );
        CREATE INDEX IF NOT EXISTS idx_provider_runs_recent
            ON provider_runs(provider, chain, recorded_epoch DESC);

        CREATE TABLE IF NOT EXISTS wallet_monitor (
            wallet_key TEXT PRIMARY KEY,
            chain TEXT NOT NULL,
            address TEXT NOT NULL,
            last_validated_at TEXT,
            last_validated_epoch INTEGER,
            last_activity_marker INTEGER NOT NULL DEFAULT 0,
            next_refresh_epoch INTEGER NOT NULL DEFAULT 0,
            last_score REAL,
            last_gate TEXT
        );
        """
    )
    conn.commit()
    return conn


def _ratio(value, *, strict: bool = False) -> tuple[float | None, str | None]:
    number = _float(value)
    if number is None:
        return None, None
    if 0.0 <= number <= 1.0:
        return number, None
    if not strict and 1.0 < number <= 100.0:
        return number / 100.0, "converted_percent_points_to_ratio"
    return None, "invalid_ratio_unit"


def normalize_metrics(provider: str, metrics: dict) -> tuple[dict, list[str]]:
    provider = str(provider or "INLINE").upper()
    out: dict[str, float | int] = {}
    issues: list[str] = []

    raw_wr = metrics.get("win_rate")
    if raw_wr is None:
        raw_wr = metrics.get("gmgn_winrate_30d")
    if provider == "NANSEN":
        wr = strict_ratio(raw_wr)
        issue = "invalid_ratio_unit" if wr is None and _float(raw_wr) is not None else None
    else:
        wr = ratio_from_any(raw_wr)
        number = _float(raw_wr)
        if wr is None and number is not None:
            issue = "invalid_ratio_unit"
        elif wr is not None and number is not None and number > 1.0:
            issue = "converted_percent_points_to_ratio"
        else:
            issue = None
    if wr is not None:
        out["win_rate"] = wr
        out["gmgn_winrate_30d"] = wr
    if issue:
        issues.append(f"win_rate:{issue}")

    roi = _float(metrics.get("realized_roi_30d"))
    roi_unit = str(metrics.get(ROI_UNIT_KEY) or "").strip().lower()
    if roi is not None:
        if roi_unit == RATIO:
            # Adaptador já converteu e marcou a unidade: confiar.
            out["realized_roi_30d"] = roi
        elif roi_unit == "percent":
            out["realized_roi_30d"] = roi / 100.0
        elif provider == "NANSEN":
            # Cache antigo do Nansen: realized_pnl_percent bruto.
            out["realized_roi_30d"] = roi / 100.0
            issues.append("realized_roi_30d:nansen_percent_points_to_ratio")
        elif provider == "ZERION":
            if str(metrics.get("zerion_roi_unit", "")).strip().lower() == "ratio":
                out["realized_roi_30d"] = roi
            else:
                issues.append("realized_roi_30d:zerion_unit_ambiguous_ignored")
        elif provider in {"GMGN_CACHE", "BIRDEYE"}:
            # GMGN (V6 validado) e Birdeye já entregam razão.
            out["realized_roi_30d"] = roi
        elif abs(roi) <= 1.0:
            out["realized_roi_30d"] = roi
        elif abs(roi) <= 10000.0:
            out["realized_roi_30d"] = roi / 100.0
            issues.append("realized_roi_30d:converted_percent_points_to_ratio")

    for name in (
        "realized_profit_30d", "median_pnl_per_token", "profit_hhi",
        "largest_win_share", "top3_profit_share", "new_positions_per_week",
        "temporal_consistency_score", "positive_active_weeks", "active_weeks",
    ):
        value = _float(metrics.get(name))
        if value is not None:
            out[name] = value

    for name in ("closed_positions", "total_trades", "winning_tokens", "tokens_traded"):
        value = _float(metrics.get(name))
        if value is not None and value >= 0:
            out[name] = int(round(value))

    repeat, issue = _ratio(metrics.get("repeatability_score"), strict=False)
    if repeat is not None:
        out["repeatability_score"] = repeat
    if issue:
        issues.append(f"repeatability_score:{issue}")

    return out, sorted(set(issues))


def _unit(metric: str) -> str:
    if metric in {
        "win_rate", "gmgn_winrate_30d", "realized_roi_30d", "repeatability_score",
        "profit_hhi", "largest_win_share", "top3_profit_share", "temporal_consistency_score",
    }:
        return "ratio"
    if metric in {"realized_profit_30d", "median_pnl_per_token"}:
        return "usd"
    if metric == "new_positions_per_week":
        return "positions_per_week"
    return "count"


def _same_value(a, b) -> bool:
    try:
        return abs(float(a) - float(b)) <= 1e-12 * max(1.0, abs(float(a)), abs(float(b)))
    except (TypeError, ValueError):
        return False


def record_metrics(
    db_path: Path,
    *,
    chain: str,
    address: str,
    provider: str,
    metrics: dict,
    observed_at: str | None = None,
    window_days: int | None = 30,
    methodology: str | None = None,
    source_quality: float = 1.0,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Grava observações; valor repetido só atualiza a data da última linha.

    Assim o histórico cresce apenas quando o valor muda, e a data continua
    refletindo a confirmação mais recente.
    """
    chain = normalize_chain(chain)
    address = normalize_address(chain, address)
    if not address:
        return {"recorded": 0, "inserted": 0, "refreshed": 0, "issues": ["missing_address"]}
    normalized, issues = normalize_metrics(provider, metrics)
    when = str(observed_at or _now_iso())
    try:
        epoch = int(datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp())
    except Exception:
        when = _now_iso()
        epoch = int(time.time())
    issue_text = ";".join(issues)
    key = wallet_key(chain, address)
    provider = str(provider).upper()
    methodology = methodology or ""
    own_conn = conn is None
    if own_conn:
        conn = _connect(db_path)
    inserted = refreshed = 0
    try:
        for metric, value in normalized.items():
            latest = conn.execute(
                """
                SELECT rowid, value, observed_epoch, methodology, window_days
                FROM wallet_evidence
                WHERE wallet_key=? AND provider=? AND metric=?
                ORDER BY observed_epoch DESC LIMIT 1
                """,
                (key, provider, metric),
            ).fetchone()
            if (
                latest is not None
                and _same_value(latest[1], value)
                and str(latest[3] or "") == methodology
                and latest[4] == window_days
            ):
                if epoch > int(latest[2] or 0):
                    conn.execute(
                        """
                        UPDATE OR REPLACE wallet_evidence
                        SET observed_at=?, observed_epoch=?, source_quality=?, normalization_issue=?
                        WHERE rowid=?
                        """,
                        (when, epoch, float(source_quality), issue_text, latest[0]),
                    )
                    refreshed += 1
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO wallet_evidence
                (wallet_key, chain, address, provider, metric, value, unit,
                 observed_at, observed_epoch, window_days, methodology,
                 source_quality, normalization_issue)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key, chain, address, provider, metric,
                    float(value), _unit(metric), when, epoch, window_days,
                    methodology, float(source_quality), issue_text,
                ),
            )
            inserted += 1
        if own_conn:
            conn.commit()
    finally:
        if own_conn:
            conn.close()
    return {"recorded": inserted + refreshed, "inserted": inserted, "refreshed": refreshed, "issues": issues}


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def sync_provider_caches(state_dir: Path, db_path: Path) -> dict:
    patterns = (
        ("NANSEN", "nansen_pnl_*_cache.json", "Nansen 30d profiler PnL", 1.0),
        ("ZERION", "zerion_pnl_*_cache.json", "Zerion 30d wallet PnL", 0.9),
        ("COINSTATS", "coinstats_30d_*_cache.json", "CoinStats 30d realized PnL", 0.85),
        ("BIRDEYE", "birdeye_pnl_cache.json", "Birdeye wallet PnL summary", 0.8),
    )
    observations = entries_seen = files = 0
    issue_counts: dict[str, int] = {}
    conn = _connect(db_path)
    try:
        for provider, pattern, method, quality in patterns:
            for path in state_dir.glob(pattern):
                files += 1
                name = path.name
                if provider == "NANSEN":
                    chain = name[len("nansen_pnl_"):-len("_cache.json")]
                elif provider == "ZERION":
                    chain = name[len("zerion_pnl_"):-len("_cache.json")]
                elif provider == "BIRDEYE":
                    chain = "solana"
                else:
                    chain = name[len("coinstats_30d_"):-len("_cache.json")]
                payload = _load_json(path)
                key = "wallets" if provider == "BIRDEYE" else "entries"
                entries = payload.get(key) if isinstance(payload.get(key), dict) else {}
                for address, item in entries.items():
                    if not isinstance(item, dict) or not isinstance(item.get("metrics"), dict) or not item["metrics"]:
                        continue
                    entries_seen += 1
                    result = record_metrics(
                        db_path,
                        chain=chain,
                        address=str(address),
                        provider=provider,
                        metrics=item["metrics"],
                        observed_at=str(item.get("checked_at") or _now_iso()),
                        window_days=30,
                        methodology=method,
                        source_quality=quality,
                        conn=conn,
                    )
                    observations += int(result.get("inserted", 0))
                    for issue in result.get("issues", []):
                        issue_counts[issue] = issue_counts.get(issue, 0) + 1
        conn.commit()
    finally:
        conn.close()
    return {
        "status": "DONE", "cache_files": files, "wallet_entries": entries_seen,
        "observations": observations, "normalization_issues": issue_counts,
    }


def canonical_metrics_for_wallet(
    db_path: Path,
    chain: str,
    address: str,
    *,
    conn: sqlite3.Connection | None = None,
    max_age_days: float | None = DEFAULT_EVIDENCE_MAX_AGE_DAYS,
    now_epoch: int | None = None,
) -> dict:
    """Escolhe o valor canônico de cada métrica.

    Dentro da janela de frescor vale a prioridade do provedor; uma observação
    velha só é usada quando não existe nenhuma recente para aquela métrica.
    """
    key = wallet_key(chain, address)
    own_conn = conn is None
    if own_conn:
        conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT provider, metric, value, observed_epoch, source_quality, normalization_issue
            FROM wallet_evidence WHERE wallet_key=?
            ORDER BY observed_epoch DESC
            """,
            (key,),
        ).fetchall()
    finally:
        if own_conn:
            conn.close()

    latest: dict[tuple[str, str], tuple] = {}
    for row in rows:
        latest.setdefault((str(row[0]), str(row[1])), row)
    by_metric: dict[str, list[tuple]] = {}
    for row in latest.values():
        by_metric.setdefault(str(row[1]), []).append(row)

    now = int(time.time()) if now_epoch is None else int(now_epoch)
    max_age = None if max_age_days is None or max_age_days <= 0 else float(max_age_days) * 86400.0
    out: dict = {"wallet_key": key}
    sources: set[str] = set()
    all_issues: set[str] = set()
    stale_metrics = 0
    pools: dict[str, list[tuple]] = {}
    for metric, observations in by_metric.items():
        fresh = [r for r in observations if max_age is None or now - int(r[3] or 0) <= max_age]
        pool = fresh or observations
        if not fresh:
            stale_metrics += 1
        pool.sort(
            key=lambda row: (
                _PROVIDER_PRIORITY.get(str(row[0]).upper(), 0),
                float(row[4] or 0.0), int(row[3] or 0),
            ),
            reverse=True,
        )
        pools[metric] = pool
        chosen = pool[0]
        out[metric] = float(chosen[2])
        for row in pool:
            sources.add(str(row[0]).upper())
            issue = str(row[5] or "")
            if issue:
                all_issues.update(x for x in issue.split(";") if x)

    wr_values = [float(row[2]) for row in pools.get("win_rate", [])]
    roi_values = [float(row[2]) for row in pools.get("realized_roi_30d", [])]
    wr_spread = max(wr_values) - min(wr_values) if len(wr_values) >= 2 else 0.0
    roi_spread = max(roi_values) - min(roi_values) if len(roi_values) >= 2 else 0.0
    confidence = 1.0
    if len(wr_values) >= 2:
        confidence *= max(0.4, 1.0 - min(1.0, wr_spread / 0.40))
    if len(roi_values) >= 2:
        confidence *= max(0.5, 1.0 - min(1.0, roi_spread / 5.0))
    out.update({
        "evidence_sources": ",".join(sorted(sources)),
        "evidence_source_count": len(sources),
        "evidence_confidence": round(confidence, 4),
        "evidence_disagreement_wr": round(wr_spread, 4),
        "evidence_disagreement_roi": round(roi_spread, 4),
        "evidence_stale_metrics": int(stale_metrics),
        "metric_normalization_issues": ";".join(sorted(all_issues)),
    })
    return out


def prune_evidence(
    db_path: Path,
    *,
    retention_days: float = DEFAULT_EVIDENCE_RETENTION_DAYS,
    conn: sqlite3.Connection | None = None,
    now_epoch: int | None = None,
) -> dict:
    """Apaga histórico antigo, preservando a última observação de cada métrica."""
    if retention_days is None or retention_days <= 0:
        return {"status": "DISABLED", "deleted": 0}
    now = int(time.time()) if now_epoch is None else int(now_epoch)
    cutoff = now - int(float(retention_days) * 86400)
    own_conn = conn is None
    if own_conn:
        conn = _connect(db_path)
    try:
        deleted = conn.execute(
            """
            DELETE FROM wallet_evidence WHERE rowid IN (
                SELECT rowid FROM (
                    SELECT rowid, observed_epoch,
                           ROW_NUMBER() OVER (
                               PARTITION BY wallet_key, provider, metric
                               ORDER BY observed_epoch DESC
                           ) AS rn
                    FROM wallet_evidence
                ) WHERE rn > 1 AND observed_epoch < ?
            )
            """,
            (cutoff,),
        ).rowcount
        runs_deleted = conn.execute(
            "DELETE FROM provider_runs WHERE recorded_epoch < ?", (cutoff,)
        ).rowcount
        conn.commit()
    finally:
        if own_conn:
            conn.close()
    return {"status": "DONE", "deleted": int(deleted or 0), "provider_runs_deleted": int(runs_deleted or 0)}


def record_provider_run(
    db_path: Path, *, provider: str, chain: str, attempted: int = 0,
    enriched: int = 0, errors: int = 0, http_calls: int = 0,
    credits: float = 0.0, statuses: list[int] | None = None,
) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO provider_runs
            (recorded_at, recorded_epoch, provider, chain, attempted, enriched,
             errors, http_calls, credits, statuses)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _now_iso(), int(time.time()), str(provider).upper(), normalize_chain(chain),
                max(0, int(attempted)), max(0, int(enriched)), max(0, int(errors)),
                max(0, int(http_calls)), float(credits or 0.0),
                json.dumps(sorted(set(int(x) for x in (statuses or []) if x is not None))),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def provider_health(db_path: Path, provider: str, chain: str | None = None, *, hours: int = 24) -> dict:
    cutoff = int(time.time()) - max(1, int(hours)) * 3600
    conn = _connect(db_path)
    try:
        if chain:
            rows = conn.execute(
                "SELECT attempted,enriched,errors,http_calls,credits,statuses FROM provider_runs WHERE provider=? AND chain=? AND recorded_epoch>=?",
                (str(provider).upper(), normalize_chain(chain), cutoff),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT attempted,enriched,errors,http_calls,credits,statuses FROM provider_runs WHERE provider=? AND recorded_epoch>=?",
                (str(provider).upper(), cutoff),
            ).fetchall()
    finally:
        conn.close()
    attempted = sum(int(r[0] or 0) for r in rows)
    enriched = sum(int(r[1] or 0) for r in rows)
    errors = sum(int(r[2] or 0) for r in rows)
    calls = sum(int(r[3] or 0) for r in rows)
    credits = sum(float(r[4] or 0.0) for r in rows)
    statuses: list[int] = []
    for row in rows:
        try:
            statuses.extend(int(x) for x in json.loads(row[5] or "[]"))
        except Exception:
            pass
    return {
        "provider": str(provider).upper(),
        "chain": normalize_chain(chain) if chain else "all",
        "attempted": attempted,
        "enriched": enriched,
        "errors": errors,
        "http_calls": calls,
        "credits": round(credits, 4),
        "success_rate": None if not attempted else round(enriched / attempted, 4),
        "throttled_statuses": sum(1 for x in statuses if x in {429, 503}),
    }


def monitor_state_map(db_path: Path) -> dict[str, dict]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT wallet_key,chain,address,last_validated_epoch,last_activity_marker,next_refresh_epoch,last_score,last_gate FROM wallet_monitor"
        ).fetchall()
    finally:
        conn.close()
    return {
        str(r[0]): {
            "chain": r[1],
            "address": r[2],
            "last_validated_epoch": int(r[3] or 0),
            "last_activity_marker": int(r[4] or 0),
            "next_refresh_epoch": int(r[5] or 0),
            "last_score": r[6],
            "last_gate": r[7],
        }
        for r in rows
    }
