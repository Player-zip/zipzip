from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import time

import pandas as pd

from .nansen_evm import _atomic_json, _fetch_wallet, _load_json
from .zerion_evm import fetch_zerion_wallet_pnl


def _has_value(row: dict, key: str) -> bool:
    value = row.get(key)
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except Exception:
        pass
    text = str(value).strip().lower()
    return text not in {"", "nan", "none", "null"}


def _has_performance_evidence(row: dict) -> bool:
    return _has_value(row, "win_rate") or _has_value(row, "gmgn_winrate_30d")


def _eligible(row: dict) -> bool:
    value = row.get("eligible_enrichment")
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


def _fill_missing(row: dict, metrics: dict) -> None:
    for key, value in metrics.items():
        if value is None:
            continue
        if not _has_value(row, key):
            row[key] = value


def _number(row: dict, key: str, default: float = 0.0) -> float:
    try:
        value = row.get(key)
        if value is None or pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def _priority_key(row: dict) -> tuple:
    """Spend provider calls first where one more fact is most likely useful."""
    priority_rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}.get(str(row.get("priority", "")).upper(), 9)
    evidence_fields = (
        "realized_profit_30d",
        "realized_roi_30d",
        "closed_positions",
        "total_trades",
        "repeatability_score",
        "new_positions_per_week",
    )
    completeness = sum(1 for key in evidence_fields if _has_value(row, key))
    return (
        priority_rank,
        -completeness,
        -_number(row, "discovery_score"),
        -_number(row, "distinct_tokens"),
        -_number(row, "sampled_txs"),
    )


def _cooldown_ok(attempts: dict, address: str, now: int, cooldown: int) -> bool:
    attempt = attempts.get(address) if isinstance(attempts.get(address), dict) else {}
    last_epoch = int(attempt.get("last_attempt_epoch", 0) or 0)
    return not last_epoch or now - last_epoch >= cooldown


def _record_attempt(attempts: dict, address: str, now: int, metrics, error_text: str, statuses: list[int]) -> None:
    attempts[address] = {
        "last_attempt_epoch": now,
        "last_attempt_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "DONE" if isinstance(metrics, dict) else "ERROR",
        "error": error_text,
        "http_statuses": statuses,
    }


def enrich_legacy_robinhood_rotating(
    input_path: Path,
    output_path: Path,
    state_dir: Path,
    *,
    api_key: str | None,
    zerion_api_key: str | None = None,
    timeout: float = 15.0,
    lookback_days: int = 30,
    batch_size: int = 20,
    zerion_batch_size: int = 40,
    retry_cooldown_seconds: int = 86400,
    delay: float = 0.15,
) -> dict:
    """Enrich deferred Robinhood EOAs with a priority-aware provider router.

    Nansen remains the first source because it already gives WR plus per-token PnL.
    Zerion then works on the still-deferred wallets in a separate bounded batch.
    Zerion is only allowed to create WR/sample evidence when its realized per-asset
    breakdown contains actual closed positions. Provider observations are cached
    and only fill missing fields; the Alpha gates themselves are unchanged.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    if not input_path.is_file():
        pd.DataFrame().to_csv(output_path, index=False)
        return {"status": "NO_INPUT", "wallets": 0, "newly_enriched": 0, "http_calls": 0}

    try:
        frame = pd.read_csv(input_path)
    except Exception:
        frame = pd.DataFrame()

    if frame.empty or "address" not in frame.columns:
        frame.to_csv(output_path, index=False)
        return {"status": "DONE_EMPTY", "wallets": 0, "newly_enriched": 0, "http_calls": 0}

    rows = frame.to_dict("records")
    for row in rows:
        row["chain"] = "robinhood"
        row["chain_id"] = 4663

    nansen_key = str(api_key or "").strip()
    zerion_key = str(zerion_api_key or "").strip()
    if not nansen_key and not zerion_key:
        pd.DataFrame(rows).to_csv(output_path, index=False)
        return {
            "status": "NOT_CONFIGURED",
            "chain": "robinhood",
            "wallets": int(len(rows)),
            "newly_enriched": 0,
            "http_calls": 0,
        }

    nansen_cache_path = state_dir / "nansen_pnl_robinhood_cache.json"
    nansen_cache = _load_json(nansen_cache_path)
    nansen_entries = nansen_cache.get("entries") if isinstance(nansen_cache.get("entries"), dict) else {}

    zerion_cache_path = state_dir / "zerion_pnl_robinhood_cache.json"
    zerion_cache = _load_json(zerion_cache_path)
    zerion_entries = zerion_cache.get("entries") if isinstance(zerion_cache.get("entries"), dict) else {}

    rotation_path = state_dir / "provider_legacy_robinhood_rotation.json"
    rotation = _load_json(rotation_path)
    nansen_attempts = rotation.get("nansen_attempts") if isinstance(rotation.get("nansen_attempts"), dict) else {}
    zerion_attempts = rotation.get("zerion_attempts") if isinstance(rotation.get("zerion_attempts"), dict) else {}

    # Migration from the first Nansen-only rotation file, so successful progress
    # and cooldowns are not discarded after enabling the provider router.
    old_rotation = _load_json(state_dir / "nansen_legacy_robinhood_rotation.json")
    old_attempts = old_rotation.get("attempts") if isinstance(old_rotation.get("attempts"), dict) else {}
    for address, attempt in old_attempts.items():
        nansen_attempts.setdefault(address, attempt)

    now = int(time.time())
    already_evidenced_before = sum(1 for row in rows if _has_performance_evidence(row))
    nansen_cached_applied = 0
    zerion_cached_applied = 0

    # Reuse every paid observation. Existing GMGN data wins; providers only fill
    # gaps, which avoids silently changing a metric's historical source.
    for row in rows:
        address = str(row.get("address", "") or "").strip().lower()
        if not address:
            continue
        for provider, entries in (("nansen", nansen_entries), ("zerion", zerion_entries)):
            cached = entries.get(address) if isinstance(entries.get(address), dict) else {}
            metrics = cached.get("metrics") if isinstance(cached.get("metrics"), dict) else None
            if not metrics:
                continue
            had = _has_performance_evidence(row)
            _fill_missing(row, metrics)
            if not had and _has_performance_evidence(row):
                if provider == "nansen":
                    nansen_cached_applied += 1
                else:
                    zerion_cached_applied += 1
            if _has_performance_evidence(row):
                row["wallet_evidence_state"] = "PERFORMANCE_ENRICHED"

    eligible_missing_before = sum(1 for row in rows if _eligible(row) and not _has_performance_evidence(row))
    cooldown = max(0, int(retry_cooldown_seconds))

    candidates = [
        row for row in rows
        if _eligible(row)
        and not _has_performance_evidence(row)
        and len(str(row.get("address", "") or "").strip()) == 42
        and str(row.get("address", "") or "").strip().lower().startswith("0x")
    ]
    candidates.sort(key=_priority_key)

    nansen_selected: list[dict] = []
    nansen_cooldown_skipped = 0
    if nansen_key:
        for row in candidates:
            if len(nansen_selected) >= max(0, int(batch_size)):
                break
            address = str(row.get("address", "") or "").strip().lower()
            if not _cooldown_ok(nansen_attempts, address, now, cooldown):
                nansen_cooldown_skipped += 1
                continue
            nansen_selected.append(row)

    nansen_new = 0
    nansen_errors = 0
    nansen_calls = 0
    nansen_credits = 0.0
    nansen_statuses: list[int] = []

    for row in nansen_selected:
        address = str(row.get("address", "") or "").strip().lower()
        try:
            metrics, meta = _fetch_wallet(
                nansen_key,
                address,
                "robinhood",
                timeout=timeout,
                lookback_days=lookback_days,
            )
        except Exception as exc:
            metrics = None
            meta = {"http_calls": 0, "statuses": [], "credits_used": 0.0}
            error_text = f"{type(exc).__name__}: {exc}"
        else:
            error_text = ""

        statuses = [int(x) for x in meta.get("statuses", []) if x is not None]
        nansen_calls += int(meta.get("http_calls", 0) or 0)
        nansen_credits += float(meta.get("credits_used", 0.0) or 0.0)
        nansen_statuses.extend(statuses)
        _record_attempt(nansen_attempts, address, now, metrics, error_text, statuses)

        if isinstance(metrics, dict):
            nansen_entries[address] = {
                "checked_epoch": now,
                "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "metrics": metrics,
            }
            had = _has_performance_evidence(row)
            _fill_missing(row, metrics)
            if not had and _has_performance_evidence(row):
                row["wallet_evidence_state"] = "PERFORMANCE_ENRICHED"
                nansen_new += 1
        else:
            nansen_errors += 1

        if delay > 0:
            time.sleep(float(delay))

    # Zerion receives the next best still-deferred rows. Nansen failures are also
    # eligible here immediately, making Zerion a true fallback rather than making
    # a promising wallet wait another six-hour cycle.
    remaining = [row for row in candidates if not _has_performance_evidence(row)]
    remaining.sort(key=_priority_key)
    zerion_selected: list[dict] = []
    zerion_cooldown_skipped = 0
    if zerion_key:
        for row in remaining:
            if len(zerion_selected) >= max(0, int(zerion_batch_size)):
                break
            address = str(row.get("address", "") or "").strip().lower()
            if not _cooldown_ok(zerion_attempts, address, now, cooldown):
                zerion_cooldown_skipped += 1
                continue
            zerion_selected.append(row)

    zerion_new = 0
    zerion_pnl_only = 0
    zerion_errors = 0
    zerion_calls = 0
    zerion_statuses: list[int] = []

    for row in zerion_selected:
        address = str(row.get("address", "") or "").strip().lower()
        try:
            metrics, meta = fetch_zerion_wallet_pnl(
                zerion_key,
                address,
                "robinhood",
                timeout=timeout,
                lookback_days=lookback_days,
            )
        except Exception as exc:
            metrics = None
            meta = {"http_calls": 0, "statuses": []}
            error_text = f"{type(exc).__name__}: {exc}"
        else:
            error_text = str(meta.get("error", "") or "")

        statuses = [int(x) for x in meta.get("statuses", []) if x is not None]
        zerion_calls += int(meta.get("http_calls", 0) or 0)
        zerion_statuses.extend(statuses)
        _record_attempt(zerion_attempts, address, now, metrics, error_text, statuses)

        if isinstance(metrics, dict):
            zerion_entries[address] = {
                "checked_epoch": now,
                "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "metrics": metrics,
            }
            had = _has_performance_evidence(row)
            _fill_missing(row, metrics)
            if not had and _has_performance_evidence(row):
                row["wallet_evidence_state"] = "PERFORMANCE_ENRICHED"
                zerion_new += 1
            else:
                zerion_pnl_only += 1
        else:
            zerion_errors += 1

        if delay > 0:
            time.sleep(float(delay))

    _atomic_json(nansen_cache_path, {"entries": nansen_entries})
    _atomic_json(zerion_cache_path, {"entries": zerion_entries})
    _atomic_json(rotation_path, {
        "nansen_attempts": nansen_attempts,
        "zerion_attempts": zerion_attempts,
    })

    out = pd.DataFrame(rows)
    out.to_csv(output_path, index=False)

    evidenced_after = sum(1 for row in rows if _has_performance_evidence(row))
    eligible_missing_after = sum(1 for row in rows if _eligible(row) and not _has_performance_evidence(row))
    total_new = max(0, evidenced_after - already_evidenced_before)
    total_errors = nansen_errors + zerion_errors

    if total_errors == 0:
        status = "DONE"
    elif total_new or nansen_cached_applied or zerion_cached_applied:
        status = "PARTIAL"
    else:
        status = "ERROR"

    return {
        "status": status,
        "chain": "robinhood",
        "provider_router": "NANSEN_THEN_ZERION",
        "wallets": int(len(rows)),
        "already_evidenced_before": int(already_evidenced_before),
        "cached_applied": int(nansen_cached_applied + zerion_cached_applied),
        "newly_enriched": int(total_new),
        "evidenced_after": int(evidenced_after),
        "eligible_missing_before": int(eligible_missing_before),
        "eligible_missing_after": int(eligible_missing_after),
        "nansen_batch_selected": int(len(nansen_selected)),
        "nansen_newly_enriched": int(nansen_new),
        "nansen_cooldown_skipped": int(nansen_cooldown_skipped),
        "nansen_errors": int(nansen_errors),
        "nansen_http_calls": int(nansen_calls),
        "nansen_credits_used": round(float(nansen_credits), 4),
        "nansen_http_statuses": sorted(set(nansen_statuses)),
        "zerion_batch_selected": int(len(zerion_selected)),
        "zerion_newly_enriched": int(zerion_new),
        "zerion_pnl_only": int(zerion_pnl_only),
        "zerion_cooldown_skipped": int(zerion_cooldown_skipped),
        "zerion_errors": int(zerion_errors),
        "zerion_http_calls": int(zerion_calls),
        "zerion_http_statuses": sorted(set(zerion_statuses)),
        "http_calls": int(nansen_calls + zerion_calls),
        "output": str(output_path),
    }
