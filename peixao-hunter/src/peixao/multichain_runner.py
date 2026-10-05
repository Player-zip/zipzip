from __future__ import annotations

from .adaptive_queue import build_adaptive_execution_queue
from .base_radar import run_base_radar
from .coinstats_enrichment import enrich_coinstats_fallback
from .config import Settings, env_float, env_int, settings
from .execution_queue import build_execution_queue
from .final_stage import FINAL_STAGE1, build_final_stage1, first_existing
from .priority_enrichment import enrich_nansen_pnl_priority
from .quicknode_solana import run_quicknode_solana_from_settings
from .robinhood_incremental import run_robinhood_radar_incremental
from .rotating_enrichment import enrich_legacy_robinhood_rotating
from .runner import run_v6 as run_v6_legacy
from .state import persist_summary, safe_call, utc_now
from .telegram_notifier import notify_alpha_wallets
from .token_radar import run_token_radar

# Mantido para quem importava o helper antigo deste módulo.
_safe = safe_call


def _quicknode_discovery(cfg: Settings) -> dict:
    return run_quicknode_solana_from_settings(cfg)


def _base_discovery(cfg: Settings, *, ttl_seconds: int) -> dict:
    return run_base_radar(
        cfg.output_dir,
        cfg.state_dir,
        dexscreener_base_url=cfg.dexscreener_base_url,
        alchemy_api_key=cfg.alchemy_api_key,
        timeout=max(cfg.rpc_timeout, 15.0),
        ttl_seconds=ttl_seconds,
        max_candidates=env_int("PEIXAO_BASE_MAX_TOKENS", cfg.token_radar_max_candidates),
        max_shortlist=env_int("PEIXAO_BASE_MAX_SHORTLIST", cfg.token_radar_max_shortlist),
        min_liquidity_usd=cfg.token_radar_min_liquidity_usd,
        min_volume_24h_usd=cfg.token_radar_min_volume_24h_usd,
        min_radar_score=cfg.token_radar_min_score,
        min_cross_token_hits=cfg.birdeye_min_cross_token_hits,
        max_wallets=env_int("PEIXAO_BASE_MAX_WALLETS", 60),
    )


def _robinhood_discovery(cfg: Settings, *, ttl_seconds: int) -> dict:
    """Varredura de logs de 6h (reconciliação/backfill do stream)."""
    return run_robinhood_radar_incremental(
        cfg.output_dir,
        cfg.state_dir,
        robinhood_rpc_url=cfg.robinhood_rpc_url,
        public_rpc_url=cfg.rpc_url,
        alchemy_api_key=cfg.alchemy_api_key,
        timeout=max(cfg.rpc_timeout, 15.0),
        ttl_seconds=ttl_seconds,
        max_candidates=env_int("PEIXAO_ROBINHOOD_MAX_TOKENS", 40),
        max_shortlist=env_int("PEIXAO_ROBINHOOD_MAX_SHORTLIST", cfg.token_radar_max_shortlist),
        min_cross_token_hits=cfg.birdeye_min_cross_token_hits,
        max_wallets=env_int("PEIXAO_ROBINHOOD_MAX_WALLETS", 100),
        lookback_blocks=env_int("PEIXAO_ROBINHOOD_LOOKBACK_BLOCKS", 150000),
        chunk_blocks=env_int("PEIXAO_ROBINHOOD_LOG_CHUNK_BLOCKS", 10000),
        max_logs_per_token=env_int("PEIXAO_ROBINHOOD_MAX_LOGS_PER_TOKEN", 1200),
        max_rps=env_float("PEIXAO_ROBINHOOD_MAX_RPS", 15.0),
    )


def _solana_token_radar(cfg: Settings, *, ttl_seconds: int) -> dict:
    return run_token_radar(
        cfg.output_dir,
        cfg.state_dir,
        cfg.master_db,
        jupiter_api_key=cfg.jupiter_api_key,
        jupiter_base_url=cfg.jupiter_base_url,
        dexscreener_base_url=cfg.dexscreener_base_url,
        timeout=cfg.jupiter_timeout,
        ttl_seconds=ttl_seconds,
        max_candidates=env_int("PEIXAO_SOLANA_MAX_TOKENS", cfg.token_radar_max_candidates),
        max_shortlist=env_int("PEIXAO_SOLANA_MAX_SHORTLIST", cfg.token_radar_max_shortlist),
        min_liquidity_usd=cfg.token_radar_min_liquidity_usd,
        min_volume_24h_usd=cfg.token_radar_min_volume_24h_usd,
        min_radar_score=cfg.token_radar_min_score,
    )


def _queue_ttl() -> int:
    return env_int("PEIXAO_EXECUTION_QUEUE_TTL_SECONDS", 7 * 86400)


def run_discovery_cycle(*, cfg: Settings = settings) -> dict:
    """Cheap/frequent discovery cycle.

    It refreshes token radars, advances RPC cursors and rebuilds the persistent
    priority queue. Paid performance APIs are deliberately excluded; the hourly
    validation cycle consumes the queue in priority order.
    """
    cfg.ensure_dirs()
    fast_ttl = max(300, env_int("PEIXAO_FAST_RADAR_TTL_SECONDS", 600))

    solana_radar = safe_call("fast_solana_radar", lambda: _solana_token_radar(cfg, ttl_seconds=fast_ttl))
    solana_wallets = safe_call("fast_quicknode_solana", lambda: _quicknode_discovery(cfg))
    base = safe_call("fast_base_radar", lambda: _base_discovery(cfg, ttl_seconds=fast_ttl))
    robinhood = safe_call("fast_robinhood_radar", lambda: _robinhood_discovery(cfg, ttl_seconds=fast_ttl))
    queue = safe_call("fast_execution_queue", lambda: build_execution_queue(
        cfg.output_dir, cfg.state_dir, stale_seconds=_queue_ttl(),
    ))

    summary = {
        "status": "DONE" if all(x.get("status") != "ERROR" for x in (solana_radar, solana_wallets, base, robinhood, queue)) else "PARTIAL",
        "solana_radar": solana_radar,
        "solana_wallets": solana_wallets,
        "base": base,
        "robinhood": robinhood,
        "execution_queue": queue,
        "finished_at": utc_now(),
    }
    persist_summary(
        cfg.state_dir,
        {"fast_discovery": summary, "fast_discovery_finished_at": summary["finished_at"]},
        {"at": utc_now(), "event": "fast_discovery_done", **summary},
    )
    return summary


def run_v6(*, cfg: Settings = settings, validate_against_reference: bool = True, top_ready: int = 100) -> dict:
    """Ciclo de 6h: V6 validado + reconciliação multichain + tabela final única."""
    result = run_v6_legacy(
        cfg=cfg,
        validate_against_reference=validate_against_reference,
        top_ready=top_ready,
    )

    # Legacy Robinhood rotation remains gradual and cache-aware.
    legacy_robinhood_nansen = safe_call("legacy_robinhood_provider_rotation", lambda: enrich_legacy_robinhood_rotating(
        cfg.output_dir / "V6_wallet_queue_enriched.csv",
        cfg.output_dir / "V22_legacy_robinhood_wallet_enriched.csv",
        cfg.state_dir,
        api_key=cfg.nansen_api_key,
        zerion_api_key=cfg.zerion_api_key,
        timeout=max(cfg.rpc_timeout, 15.0),
        lookback_days=30,
        batch_size=env_int("PEIXAO_LEGACY_NANSEN_BATCH", 20),
        zerion_batch_size=env_int("PEIXAO_LEGACY_ZERION_BATCH", 40),
        retry_cooldown_seconds=86400,
    ))

    legacy_robinhood_coinstats = safe_call("legacy_robinhood_coinstats", lambda: enrich_coinstats_fallback(
        cfg.output_dir / "V22_legacy_robinhood_wallet_enriched.csv",
        cfg.state_dir,
        api_key=cfg.coinstats_api_key,
        chain="robinhood",
        timeout=max(cfg.rpc_timeout, 15.0),
        lookback_days=30,
        max_batch_size=env_int("PEIXAO_COINSTATS_BATCH", 8),
        retry_cooldown_seconds=86400,
    ))

    if not cfg.token_radar_enabled:
        result["legacy_robinhood_nansen"] = legacy_robinhood_nansen
        result["legacy_robinhood_coinstats"] = legacy_robinhood_coinstats
        result["multichain"] = {
            "status": "DISABLED",
            "legacy_robinhood_nansen": legacy_robinhood_nansen,
            "legacy_robinhood_coinstats": legacy_robinhood_coinstats,
        }
        return result

    base = safe_call("base_radar", lambda: _base_discovery(cfg, ttl_seconds=cfg.token_radar_ttl_seconds))
    robinhood = safe_call("robinhood_radar", lambda: _robinhood_discovery(cfg, ttl_seconds=cfg.token_radar_ttl_seconds))

    # Mesma fila adaptativa do ciclo horário, para o Nansen gastar na mesma ordem.
    queue = safe_call("execution_priority_queue", lambda: build_adaptive_execution_queue(
        cfg.output_dir, cfg.state_dir, cfg.master_db, stale_seconds=_queue_ttl(),
    ))

    live_nansen_per_chain = env_int("PEIXAO_NANSEN_WALLETS_PER_CHAIN", 20)
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
        max_wallets=live_nansen_per_chain,
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
        max_wallets=live_nansen_per_chain,
    ))

    # Única tabela final (com Dune, identidade chain:address e unidades normalizadas).
    final = build_final_stage1(cfg)

    telegram = {"status": "SKIPPED_FINAL_STAGE_ERROR", "sent": 0}
    if final.get("status") not in {"ERROR", "NO_BASELINE"}:
        telegram = safe_call("multichain_telegram", lambda: notify_alpha_wallets(
            cfg.output_dir / FINAL_STAGE1,
            cfg.master_db,
            token=cfg.telegram_bot_token,
            chat_id=cfg.telegram_chat_id,
            enabled=cfg.telegram_alerts_enabled,
            timeout=cfg.telegram_timeout,
            access_password=cfg.telegram_access_password,
            auth_ttl_days=cfg.telegram_auth_ttl_days,
        ))

    critical = (legacy_robinhood_nansen, base, robinhood, queue, base_nansen, robinhood_nansen)
    multichain = {
        "status": "DONE" if final.get("status") == "DONE" and all(x.get("status") != "ERROR" for x in critical) else "PARTIAL",
        "legacy_robinhood_nansen": legacy_robinhood_nansen,
        "legacy_robinhood_coinstats": legacy_robinhood_coinstats,
        "base": base,
        "base_nansen": base_nansen,
        "robinhood": robinhood,
        "robinhood_nansen": robinhood_nansen,
        "execution_queue": queue,
        "final_stage": final,
        "telegram": telegram,
        "dune_required": False,
    }

    result.update({
        "legacy_robinhood_nansen": legacy_robinhood_nansen,
        "legacy_robinhood_coinstats": legacy_robinhood_coinstats,
        "base_radar": base,
        "base_nansen": base_nansen,
        "robinhood_radar": robinhood,
        "robinhood_nansen": robinhood_nansen,
        "execution_queue": queue,
        "final_stage": final,
        "selective_alpha": final.get("selective_alpha"),
        "selective_db": final.get("selective_db"),
        "telegram": telegram,
        "multichain": multichain,
    })

    persist_summary(
        cfg.state_dir,
        {
            "legacy_robinhood_nansen": legacy_robinhood_nansen,
            "legacy_robinhood_coinstats": legacy_robinhood_coinstats,
            "base_radar": base,
            "base_nansen": base_nansen,
            "robinhood_radar": robinhood,
            "robinhood_nansen": robinhood_nansen,
            "execution_queue": queue,
            "final_stage": final,
            "telegram": telegram,
            "multichain": multichain,
            "last_run_status": "DONE" if multichain["status"] == "DONE" else "PARTIAL",
            "last_run_finished_at": utc_now(),
        },
        {"at": utc_now(), "event": "multichain_run_done", **multichain},
    )
    return result


def run(tokens: list[str] | None = None, wallets: list[str] | None = None, *, cfg: Settings = settings) -> dict:
    if tokens or wallets:
        return {
            "status": "QUEUED_INPUT_NOT_YET_BOUND_TO_DISCOVERY",
            "tokens": tokens or [],
            "wallets": wallets or [],
            "message": "A fila manual ainda não faz bypass dos gates; use o radar multichain normal.",
        }
    return run_v6(cfg=cfg)
