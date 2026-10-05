from __future__ import annotations

import os

from .adaptive_queue import adaptive_provider_limit, build_adaptive_execution_queue
from .backtest_v23 import update_backtest
from .chain_aware_inputs import build_chain_aware_inputs
from .config import Settings, settings
from .evidence_ledger import sync_provider_caches
from .monitor_state_v23 import update_monitor_from_stage
from .observability_v23 import publish_efficiency_snapshot, record_priority_provider_stats
from .priority_enrichment import enrich_nansen_pnl_priority
from .selective_alpha import build_selective_stage1
from .selective_db import record_selective_stage1_csv
from .state import PipelineState, utc_now
from .telegram_notifier import notify_alpha_wallets


def _safe(name, fn):
    try:
        return fn()
    except Exception as exc:
        return {"status": "ERROR", "stage": name, "error": f"{type(exc).__name__}: {exc}"}


def _first_existing(*paths):
    for path in paths:
        if path is not None and path.is_file():
            return path
    return paths[-1] if paths else None


def run_priority_validation_cycle(*, cfg: Settings = settings) -> dict:
    """Hourly validation on the current priority queue only.

    The V2.3 delta adds evidence normalization, chain+address identity, evidence-gap
    ordering, watchlist suppression, provider health sizing and replay metrics.
    It deliberately keeps the existing 30m/1h/6h scheduler and Nansen cooldowns.
    """
    cfg.ensure_dirs()
    db_path = cfg.data_dir / "peixao_master.sqlite3"

    queue = _safe("adaptive_execution_queue", lambda: build_adaptive_execution_queue(
        cfg.output_dir,
        cfg.state_dir,
        db_path,
        stale_seconds=int(os.getenv("PEIXAO_EXECUTION_QUEUE_TTL_SECONDS", str(7 * 86400))),
    ))

    due_by_chain = queue.get("due_by_chain") if isinstance(queue.get("due_by_chain"), dict) else {}
    base_limit_default = max(0, int(os.getenv("PEIXAO_NANSEN_WALLETS_PER_CHAIN", "20")))
    base_limit = adaptive_provider_limit(
        db_path, "NANSEN", "base", base_limit_default,
        backlog=int(due_by_chain.get("base", 0) or 0),
    )
    robinhood_limit = adaptive_provider_limit(
        db_path, "NANSEN", "robinhood", base_limit_default,
        backlog=int(due_by_chain.get("robinhood", 0) or 0),
    )

    base_priority = _first_existing(
        cfg.output_dir / "V22_base_wallet_priority.csv",
        cfg.output_dir / "V22_base_wallet_candidates.csv",
    )
    robinhood_priority = _first_existing(
        cfg.output_dir / "V22_robinhood_wallet_priority.csv",
        cfg.output_dir / "V22_robinhood_wallet_candidates.csv",
    )

    base_nansen = _safe("base_nansen_pnl", lambda: enrich_nansen_pnl_priority(
        base_priority,
        cfg.output_dir / "V22_base_wallet_enriched.csv",
        cfg.state_dir,
        api_key=cfg.nansen_api_key,
        chain="base",
        timeout=max(cfg.rpc_timeout, 15.0),
        lookback_days=30,
        ttl_seconds=cfg.birdeye_pnl_ttl_seconds,
        max_wallets=base_limit,
    ))
    robinhood_nansen = _safe("robinhood_nansen_pnl", lambda: enrich_nansen_pnl_priority(
        robinhood_priority,
        cfg.output_dir / "V22_robinhood_wallet_enriched.csv",
        cfg.state_dir,
        api_key=cfg.nansen_api_key,
        chain="robinhood",
        timeout=max(cfg.rpc_timeout, 15.0),
        lookback_days=30,
        ttl_seconds=cfg.birdeye_pnl_ttl_seconds,
        max_wallets=robinhood_limit,
    ))

    provider_stats = _safe("provider_stats", lambda: record_priority_provider_stats(
        db_path,
        base_nansen=base_nansen,
        robinhood_nansen=robinhood_nansen,
    ))
    cache_sync = _safe("evidence_cache_sync", lambda: sync_provider_caches(cfg.state_dir, db_path))

    legacy_input = _first_existing(
        cfg.output_dir / "V22_legacy_robinhood_wallet_enriched.csv",
        cfg.output_dir / "V6_wallet_queue_enriched.csv",
    )
    if legacy_input is None or not legacy_input.is_file():
        return {
            "status": "NO_BASELINE",
            "queue": queue,
            "base_nansen": base_nansen,
            "robinhood_nansen": robinhood_nansen,
            "provider_stats": provider_stats,
            "cache_sync": cache_sync,
            "finished_at": utc_now(),
        }

    solana_input = _first_existing(
        cfg.output_dir / "V22_radar_wallet_enriched_quicknode.csv",
        cfg.output_dir / "V22_quicknode_wallet_candidates.csv",
        cfg.output_dir / "V22_radar_wallet_enriched.csv",
    )
    canonical_path = cfg.output_dir / "V23_canonical_wallet_inputs.csv"
    canonical = _safe("chain_aware_canonical_inputs", lambda: build_chain_aware_inputs(
        legacy_path=legacy_input,
        solana_path=solana_input,
        base_path=cfg.output_dir / "V22_base_wallet_enriched.csv",
        robinhood_path=cfg.output_dir / "V22_robinhood_wallet_enriched.csv",
        output_path=canonical_path,
        db_path=db_path,
    ))

    selective = _safe("multichain_selective_alpha", lambda: build_selective_stage1(
        legacy_input,
        None,
        cfg.output_dir,
        input_override=canonical_path,
        artifact_prefix="V22S",
        max_deep_dive=30,
    ))
    selective_db = _safe("multichain_selective_db", lambda: record_selective_stage1_csv(
        cfg.output_dir / "V22S_wallet_stage1.csv",
        db_path,
    ))

    backtest = _safe("score_replay", lambda: update_backtest(
        cfg.output_dir / "V22S_wallet_stage1.csv",
        db_path,
        cfg.output_dir / "V22_backtest_summary.csv",
    ))
    monitor_state = _safe("monitor_state", lambda: update_monitor_from_stage(
        db_path,
        cfg.output_dir / "V22S_wallet_stage1.csv",
        cfg.output_dir / "V22_execution_queue.csv",
        refresh_seconds=int(os.getenv("PEIXAO_MONITOR_REFRESH_SECONDS", "86400")),
    ))
    observability = _safe("efficiency_snapshot", lambda: publish_efficiency_snapshot(
        output_dir=cfg.output_dir,
        state_dir=cfg.state_dir,
        db_path=db_path,
        adaptive_queue=queue,
        backtest=backtest,
    ))

    telegram = _safe("multichain_telegram", lambda: notify_alpha_wallets(
        cfg.output_dir / "V22S_wallet_stage1.csv",
        db_path,
        token=cfg.telegram_bot_token,
        chat_id=cfg.telegram_chat_id,
        enabled=cfg.telegram_alerts_enabled,
        timeout=cfg.telegram_timeout,
    ))

    critical = (queue, base_nansen, robinhood_nansen, canonical, selective, selective_db)
    status = "DONE" if all(x.get("status") not in {"ERROR", "NO_BASELINE"} for x in critical) else "PARTIAL"
    result = {
        "status": status,
        "mode": "PRIORITY_QUEUE_V23_DELTA",
        "queue": queue,
        "base_nansen": base_nansen,
        "robinhood_nansen": robinhood_nansen,
        "provider_stats": provider_stats,
        "cache_sync": cache_sync,
        "canonical_inputs": canonical,
        "selective_alpha": selective,
        "selective_db": selective_db,
        "backtest": backtest,
        "monitor_state": monitor_state,
        "observability": observability,
        "telegram": telegram,
        "finished_at": utc_now(),
    }

    try:
        store = PipelineState(cfg.state_dir)
        state = store.load()
        state["priority_validation"] = result
        state["priority_validation_finished_at"] = result["finished_at"]
        state["v23_delta_active"] = True
        state["execution_queue"] = queue
        state["canonical_inputs"] = canonical
        state["backtest"] = backtest
        state["observability"] = observability
        store.save(state)
        store.log({"at": utc_now(), "event": "priority_validation_done", **result})
    except Exception:
        pass
    return result
