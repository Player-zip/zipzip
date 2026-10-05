from __future__ import annotations

from .adaptive_queue import adaptive_provider_limit, build_adaptive_execution_queue
from .backtest_v23 import update_backtest
from .config import Settings, env_int, settings
from .final_stage import FINAL_STAGE1, build_final_stage1, first_existing
from .monitor_state_v23 import update_monitor_from_stage
from .observability_v23 import publish_efficiency_snapshot, record_priority_provider_stats
from .priority_enrichment import enrich_nansen_pnl_priority
from .state import persist_summary, safe_call, utc_now
from .telegram_notifier import notify_alpha_wallets


def run_priority_validation_cycle(*, cfg: Settings = settings) -> dict:
    """Hourly validation on the current priority queue only.

    The V2.3 delta adds evidence normalization, chain+address identity, evidence-gap
    ordering, watchlist suppression, provider health sizing and replay metrics.
    It deliberately keeps the existing 30m/1h/6h scheduler and Nansen cooldowns.
    """
    cfg.ensure_dirs()
    db_path = cfg.master_db

    queue = safe_call("adaptive_execution_queue", lambda: build_adaptive_execution_queue(
        cfg.output_dir,
        cfg.state_dir,
        db_path,
        stale_seconds=env_int("PEIXAO_EXECUTION_QUEUE_TTL_SECONDS", 7 * 86400),
    ))

    due_by_chain = queue.get("due_by_chain") if isinstance(queue.get("due_by_chain"), dict) else {}
    base_limit_default = max(0, env_int("PEIXAO_NANSEN_WALLETS_PER_CHAIN", 20))
    base_limit = adaptive_provider_limit(
        db_path, "NANSEN", "base", base_limit_default,
        backlog=int(due_by_chain.get("base", 0) or 0),
    )
    robinhood_limit = adaptive_provider_limit(
        db_path, "NANSEN", "robinhood", base_limit_default,
        backlog=int(due_by_chain.get("robinhood", 0) or 0),
    )

    base_priority = first_existing(
        cfg.output_dir / "V22_base_wallet_priority.csv",
        cfg.output_dir / "V22_base_wallet_candidates.csv",
    )
    robinhood_priority = first_existing(
        cfg.output_dir / "V22_robinhood_wallet_priority.csv",
        cfg.output_dir / "V22_robinhood_wallet_candidates.csv",
    )

    base_nansen = safe_call("base_nansen_pnl", lambda: enrich_nansen_pnl_priority(
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
    robinhood_nansen = safe_call("robinhood_nansen_pnl", lambda: enrich_nansen_pnl_priority(
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

    provider_stats = safe_call("provider_stats", lambda: record_priority_provider_stats(
        db_path,
        base_nansen=base_nansen,
        robinhood_nansen=robinhood_nansen,
    ))
    final = build_final_stage1(cfg)
    if final.get("status") == "NO_BASELINE":
        return {
            "status": "NO_BASELINE",
            "queue": queue,
            "base_nansen": base_nansen,
            "robinhood_nansen": robinhood_nansen,
            "provider_stats": provider_stats,
            "final_stage": final,
            "finished_at": utc_now(),
        }
    stage1 = cfg.output_dir / FINAL_STAGE1

    backtest = safe_call("score_replay", lambda: update_backtest(
        stage1,
        db_path,
        cfg.output_dir / "V22_backtest_summary.csv",
    ))
    monitor_state = safe_call("monitor_state", lambda: update_monitor_from_stage(
        db_path,
        stage1,
        cfg.output_dir / "V22_execution_queue.csv",
        refresh_seconds=env_int("PEIXAO_MONITOR_REFRESH_SECONDS", 86400),
    ))
    observability = safe_call("efficiency_snapshot", lambda: publish_efficiency_snapshot(
        output_dir=cfg.output_dir,
        state_dir=cfg.state_dir,
        db_path=db_path,
        adaptive_queue=queue,
        backtest=backtest,
    ))

    # Alertas só saem de uma tabela final recém-construída.
    telegram = {"status": "SKIPPED_FINAL_STAGE_ERROR", "sent": 0} if final.get("status") == "ERROR" else safe_call("multichain_telegram", lambda: notify_alpha_wallets(
        stage1,
        db_path,
        token=cfg.telegram_bot_token,
        chat_id=cfg.telegram_chat_id,
        enabled=cfg.telegram_alerts_enabled,
        timeout=cfg.telegram_timeout,
        access_password=cfg.telegram_access_password,
        auth_ttl_days=cfg.telegram_auth_ttl_days,
    ))

    inputs_ok = all(x.get("status") != "ERROR" for x in (queue, base_nansen, robinhood_nansen))
    status = "DONE" if inputs_ok and final.get("status") == "DONE" else "PARTIAL"
    result = {
        "status": status,
        "mode": "PRIORITY_QUEUE_V23_DELTA",
        "queue": queue,
        "base_nansen": base_nansen,
        "robinhood_nansen": robinhood_nansen,
        "provider_stats": provider_stats,
        "final_stage": final,
        "backtest": backtest,
        "monitor_state": monitor_state,
        "observability": observability,
        "telegram": telegram,
        "finished_at": utc_now(),
    }

    persist_summary(
        cfg.state_dir,
        {
            "priority_validation": result,
            "priority_validation_finished_at": result["finished_at"],
            "v23_delta_active": True,
            "execution_queue": queue,
            "final_stage": final,
            "backtest": backtest,
            "observability": observability,
        },
        {"at": utc_now(), "event": "priority_validation_done", **result},
    )
    return result
