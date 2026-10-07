from __future__ import annotations

from .adaptive_queue import adaptive_provider_limit, build_adaptive_execution_queue
from .config import Settings, env_int, settings
from .final_stage import FINAL_STAGE1, build_final_stage1, first_existing
from .monitor_state_v23 import update_monitor_from_stage
from .onchain_pnl import merge_cached_metrics, run_onchain_evidence
from .observability_v23 import publish_efficiency_snapshot, record_priority_provider_stats
from .priority_enrichment import run_nansen_priority
from .score_outcomes import SUMMARY_FILE, update_score_outcomes
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
    base_limit_default = max(0, cfg.nansen_wallets_per_chain)
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

    # Evidência gratuita on-chain primeiro: resolve em paralelo o grosso do backlog
    # e poupa Nansen/Zerion (pagos) para o que o histórico on-chain não cobre.
    queue_csv = cfg.output_dir / "V22_execution_queue.csv"
    onchain: dict[str, dict] = {}
    paid_inputs: dict[str, object] = {}
    for chain, priority_path in (("base", base_priority), ("robinhood", robinhood_priority)):
        onchain[chain] = safe_call(f"{chain}_onchain_evidence", lambda c=chain, p=priority_path: run_onchain_evidence(
            cfg, c, [p, queue_csv],
        ))
        paid_inputs[chain] = merge_cached_metrics(
            priority_path,
            cfg.state_dir / f"onchain_pnl_{chain}_cache.json",
            cfg.output_dir / f"V22_{chain}_wallet_onchain.csv",
        ) if priority_path is not None else priority_path

    base_nansen = safe_call("base_nansen_pnl", lambda: run_nansen_priority(
        cfg, "base", paid_inputs["base"], max_wallets=base_limit,
    ))
    robinhood_nansen = safe_call("robinhood_nansen_pnl", lambda: run_nansen_priority(
        cfg, "robinhood", paid_inputs["robinhood"], max_wallets=robinhood_limit,
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
            "onchain": onchain,
            "base_nansen": base_nansen,
            "robinhood_nansen": robinhood_nansen,
            "provider_stats": provider_stats,
            "final_stage": final,
            "finished_at": utc_now(),
        }
    stage1 = cfg.output_dir / FINAL_STAGE1

    # Backtest honesto: sinais imutáveis por tier e resultado com observações
    # reais já coletadas (nenhuma chamada extra de API).
    backtest = safe_call("score_outcomes", lambda: update_score_outcomes(
        stage1,
        db_path,
        cfg.output_dir / SUMMARY_FILE,
    ))
    monitor_state = safe_call("monitor_state", lambda: update_monitor_from_stage(
        db_path,
        stage1,
        cfg.output_dir / "V22_execution_queue.csv",
        refresh_seconds=cfg.monitor_refresh_seconds,
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
        "onchain": onchain,
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
