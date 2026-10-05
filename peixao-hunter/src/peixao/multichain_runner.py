from __future__ import annotations

import os

from .base_radar import run_base_radar
from .coinstats_enrichment import enrich_coinstats_fallback
from .config import Settings, settings
from .evm_radar import merge_multichain_wallet_inputs
from .execution_queue import build_execution_queue
from .priority_enrichment import enrich_nansen_pnl_priority
from .quicknode_solana import run_quicknode_solana_discovery
from .robinhood_incremental import run_robinhood_radar_incremental
from .rotating_enrichment import enrich_legacy_robinhood_rotating
from .runner import run_v6 as run_v6_legacy
from .selective_alpha import build_selective_stage1
from .selective_db import record_selective_stage1_csv
from .state import PipelineState, utc_now
from .telegram_notifier import notify_alpha_wallets
from .token_radar import run_token_radar


def _safe(name, fn):
    try:
        return fn()
    except Exception as exc:
        return {"status": "ERROR", "stage": name, "error": f"{type(exc).__name__}: {exc}"}


def _quicknode_discovery(cfg: Settings) -> dict:
    enabled = str(os.getenv("PEIXAO_QUICKNODE_SOLANA", "1")).strip().lower() not in {"0", "false", "no", "off"}
    if not enabled:
        return {"status": "DISABLED", "rpc_calls": 0, "quicknode_credits": 0}
    return run_quicknode_solana_discovery(
        cfg.output_dir / "V22_token_radar_shortlist.csv",
        cfg.output_dir,
        cfg.state_dir,
        quicknode_rpc_url=os.getenv("QUICKNODE_RPC_URL"),
        helius_rpc_url=cfg.helius_rpc_url,
        shyft_rpc_url=cfg.shyft_rpc_url,
        enabled=True,
        timeout=float(os.getenv("PEIXAO_QUICKNODE_TIMEOUT", "20")),
        delay=float(os.getenv("PEIXAO_QUICKNODE_DELAY", "0.075")),
        max_tokens=int(os.getenv("PEIXAO_QUICKNODE_MAX_TOKENS", "5")),
        max_wallets=int(os.getenv("PEIXAO_QUICKNODE_MAX_WALLETS", "30")),
        tx_per_wallet=int(os.getenv("PEIXAO_QUICKNODE_TX_PER_WALLET", "80")),
        daily_credit_budget=int(os.getenv("PEIXAO_QUICKNODE_DAILY_CREDITS", "330000")),
        run_credit_budget=int(os.getenv("PEIXAO_QUICKNODE_RUN_CREDITS", "75000")),
        call_credit_estimate=int(os.getenv("PEIXAO_QUICKNODE_CALL_CREDITS", "30")),
        cache_ttl_seconds=int(os.getenv("PEIXAO_QUICKNODE_CACHE_TTL", "3600")),
    )


def _base_discovery(cfg: Settings, *, ttl_seconds: int) -> dict:
    return run_base_radar(
        cfg.output_dir,
        cfg.state_dir,
        dexscreener_base_url=cfg.dexscreener_base_url,
        alchemy_api_key=cfg.alchemy_api_key,
        timeout=max(cfg.rpc_timeout, 15.0),
        ttl_seconds=ttl_seconds,
        max_candidates=int(os.getenv("PEIXAO_BASE_MAX_TOKENS", str(cfg.token_radar_max_candidates))),
        max_shortlist=int(os.getenv("PEIXAO_BASE_MAX_SHORTLIST", str(cfg.token_radar_max_shortlist))),
        min_liquidity_usd=cfg.token_radar_min_liquidity_usd,
        min_volume_24h_usd=cfg.token_radar_min_volume_24h_usd,
        min_radar_score=cfg.token_radar_min_score,
        min_cross_token_hits=cfg.birdeye_min_cross_token_hits,
        max_wallets=int(os.getenv("PEIXAO_BASE_MAX_WALLETS", "60")),
    )


def _robinhood_discovery(cfg: Settings, *, ttl_seconds: int) -> dict:
    return run_robinhood_radar_incremental(
        cfg.output_dir,
        cfg.state_dir,
        robinhood_rpc_url=os.getenv("ROBINHOOD_RPC_URL"),
        public_rpc_url=cfg.rpc_url,
        alchemy_api_key=cfg.alchemy_api_key,
        timeout=max(cfg.rpc_timeout, 15.0),
        ttl_seconds=ttl_seconds,
        max_candidates=int(os.getenv("PEIXAO_ROBINHOOD_MAX_TOKENS", "40")),
        max_shortlist=int(os.getenv("PEIXAO_ROBINHOOD_MAX_SHORTLIST", str(cfg.token_radar_max_shortlist))),
        min_cross_token_hits=cfg.birdeye_min_cross_token_hits,
        max_wallets=int(os.getenv("PEIXAO_ROBINHOOD_MAX_WALLETS", "100")),
        lookback_blocks=int(os.getenv("PEIXAO_ROBINHOOD_LOOKBACK_BLOCKS", "150000")),
        chunk_blocks=int(os.getenv("PEIXAO_ROBINHOOD_LOG_CHUNK_BLOCKS", "10000")),
        max_logs_per_token=int(os.getenv("PEIXAO_ROBINHOOD_MAX_LOGS_PER_TOKEN", "1200")),
        max_rps=float(os.getenv("PEIXAO_ROBINHOOD_MAX_RPS", "15")),
    )


def run_discovery_cycle(*, cfg: Settings = settings) -> dict:
    """Cheap/frequent discovery cycle.

    It refreshes token radars, advances RPC cursors and rebuilds the persistent
    priority queue. Paid performance APIs are deliberately excluded; the hourly
    validation cycle consumes the queue in priority order.
    """
    cfg.ensure_dirs()
    fast_ttl = max(300, int(os.getenv("PEIXAO_FAST_RADAR_TTL_SECONDS", "600")))

    solana_radar = _safe("fast_solana_radar", lambda: run_token_radar(
        cfg.output_dir,
        cfg.state_dir,
        cfg.data_dir / "peixao_master.sqlite3",
        jupiter_api_key=cfg.jupiter_api_key,
        jupiter_base_url=cfg.jupiter_base_url,
        dexscreener_base_url=cfg.dexscreener_base_url,
        timeout=cfg.jupiter_timeout,
        ttl_seconds=fast_ttl,
        max_candidates=int(os.getenv("PEIXAO_SOLANA_MAX_TOKENS", str(cfg.token_radar_max_candidates))),
        max_shortlist=int(os.getenv("PEIXAO_SOLANA_MAX_SHORTLIST", str(cfg.token_radar_max_shortlist))),
        min_liquidity_usd=cfg.token_radar_min_liquidity_usd,
        min_volume_24h_usd=cfg.token_radar_min_volume_24h_usd,
        min_radar_score=cfg.token_radar_min_score,
    ))
    solana_wallets = _safe("fast_quicknode_solana", lambda: _quicknode_discovery(cfg))
    base = _safe("fast_base_radar", lambda: _base_discovery(cfg, ttl_seconds=fast_ttl))
    robinhood = _safe("fast_robinhood_radar", lambda: _robinhood_discovery(cfg, ttl_seconds=fast_ttl))
    queue = _safe("fast_execution_queue", lambda: build_execution_queue(
        cfg.output_dir,
        cfg.state_dir,
        stale_seconds=int(os.getenv("PEIXAO_EXECUTION_QUEUE_TTL_SECONDS", str(7 * 86400))),
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
    try:
        store = PipelineState(cfg.state_dir)
        state = store.load()
        state["fast_discovery"] = summary
        state["fast_discovery_finished_at"] = summary["finished_at"]
        store.save(state)
        store.log({"at": utc_now(), "event": "fast_discovery_done", **summary})
    except Exception:
        pass
    return summary


def run_v6(*, cfg: Settings = settings, validate_against_reference: bool = True, top_ready: int = 100) -> dict:
    """Run the validated pipeline plus priority-driven multichain validation."""
    result = run_v6_legacy(
        cfg=cfg,
        validate_against_reference=validate_against_reference,
        top_ready=top_ready,
    )

    # Legacy Robinhood rotation remains gradual and cache-aware.
    legacy_robinhood_nansen = _safe("legacy_robinhood_provider_rotation", lambda: enrich_legacy_robinhood_rotating(
        cfg.output_dir / "V6_wallet_queue_enriched.csv",
        cfg.output_dir / "V22_legacy_robinhood_wallet_enriched.csv",
        cfg.state_dir,
        api_key=cfg.nansen_api_key,
        zerion_api_key=cfg.zerion_api_key,
        timeout=max(cfg.rpc_timeout, 15.0),
        lookback_days=30,
        batch_size=int(os.getenv("PEIXAO_LEGACY_NANSEN_BATCH", "20")),
        zerion_batch_size=int(os.getenv("PEIXAO_LEGACY_ZERION_BATCH", "40")),
        retry_cooldown_seconds=86400,
    ))

    legacy_robinhood_coinstats = _safe("legacy_robinhood_coinstats", lambda: enrich_coinstats_fallback(
        cfg.output_dir / "V22_legacy_robinhood_wallet_enriched.csv",
        cfg.state_dir,
        api_key=os.getenv("COINSTATS_API_KEY"),
        chain="robinhood",
        timeout=max(cfg.rpc_timeout, 15.0),
        lookback_days=30,
        max_batch_size=int(os.getenv("PEIXAO_COINSTATS_BATCH", "8")),
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

    base = _safe("base_radar", lambda: _base_discovery(cfg, ttl_seconds=cfg.token_radar_ttl_seconds))
    robinhood = _safe("robinhood_radar", lambda: _robinhood_discovery(cfg, ttl_seconds=cfg.token_radar_ttl_seconds))

    queue = _safe("execution_priority_queue", lambda: build_execution_queue(
        cfg.output_dir,
        cfg.state_dir,
        stale_seconds=int(os.getenv("PEIXAO_EXECUTION_QUEUE_TTL_SECONDS", str(7 * 86400))),
    ))

    live_nansen_per_chain = int(os.getenv("PEIXAO_NANSEN_WALLETS_PER_CHAIN", "20"))
    base_priority = cfg.output_dir / "V22_base_wallet_priority.csv"
    if not base_priority.is_file():
        base_priority = cfg.output_dir / "V22_base_wallet_candidates.csv"
    robinhood_priority = cfg.output_dir / "V22_robinhood_wallet_priority.csv"
    if not robinhood_priority.is_file():
        robinhood_priority = cfg.output_dir / "V22_robinhood_wallet_candidates.csv"

    base_nansen = _safe("base_nansen_pnl", lambda: enrich_nansen_pnl_priority(
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

    robinhood_nansen = _safe("robinhood_nansen_pnl", lambda: enrich_nansen_pnl_priority(
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

    solana_wallet_input = cfg.output_dir / "V22_radar_wallet_enriched_quicknode.csv"
    if not solana_wallet_input.is_file():
        solana_wallet_input = cfg.output_dir / "V22_radar_wallet_enriched.csv"

    merged_path = cfg.output_dir / "V22_multichain_wallet_inputs.csv"
    merged = _safe("multichain_wallet_merge", lambda: merge_multichain_wallet_inputs(
        solana_wallet_input,
        cfg.output_dir / "V22_base_wallet_enriched.csv",
        cfg.output_dir / "V22_robinhood_wallet_enriched.csv",
        merged_path,
    ))

    legacy_input = cfg.output_dir / "V22_legacy_robinhood_wallet_enriched.csv"
    if legacy_robinhood_nansen.get("status") == "ERROR" or not legacy_input.is_file():
        legacy_input = cfg.output_dir / "V6_wallet_queue_enriched.csv"

    selective = _safe("multichain_selective_alpha", lambda: build_selective_stage1(
        legacy_input,
        merged_path,
        cfg.output_dir,
        artifact_prefix="V22S",
        max_deep_dive=30,
    ))

    selective_db = _safe("multichain_selective_db", lambda: record_selective_stage1_csv(
        cfg.output_dir / "V22S_wallet_stage1.csv",
        cfg.data_dir / "peixao_master.sqlite3",
    ))

    telegram = _safe("multichain_telegram", lambda: notify_alpha_wallets(
        cfg.output_dir / "V22S_wallet_stage1.csv",
        cfg.data_dir / "peixao_master.sqlite3",
        token=cfg.telegram_bot_token,
        chat_id=cfg.telegram_chat_id,
        enabled=cfg.telegram_alerts_enabled,
        timeout=cfg.telegram_timeout,
    ))

    critical = (legacy_robinhood_nansen, base, robinhood, queue, base_nansen, robinhood_nansen, merged, selective)
    multichain = {
        "status": "DONE" if all(x.get("status") != "ERROR" for x in critical) else "PARTIAL",
        "legacy_robinhood_nansen": legacy_robinhood_nansen,
        "legacy_robinhood_coinstats": legacy_robinhood_coinstats,
        "base": base,
        "base_nansen": base_nansen,
        "robinhood": robinhood,
        "robinhood_nansen": robinhood_nansen,
        "execution_queue": queue,
        "wallet_merge": merged,
        "selective_alpha": selective,
        "selective_db": selective_db,
        "telegram": telegram,
        "dune_required": False,
    }

    result["legacy_robinhood_nansen"] = legacy_robinhood_nansen
    result["legacy_robinhood_coinstats"] = legacy_robinhood_coinstats
    result["base_radar"] = base
    result["base_nansen"] = base_nansen
    result["robinhood_radar"] = robinhood
    result["robinhood_nansen"] = robinhood_nansen
    result["execution_queue"] = queue
    result["multichain_wallets"] = merged
    result["selective_alpha"] = selective
    result["selective_db"] = selective_db
    result["telegram"] = telegram
    result["multichain"] = multichain

    try:
        store = PipelineState(cfg.state_dir)
        state = store.load()
        state["legacy_robinhood_nansen"] = legacy_robinhood_nansen
        state["legacy_robinhood_coinstats"] = legacy_robinhood_coinstats
        state["base_radar"] = base
        state["base_nansen"] = base_nansen
        state["robinhood_radar"] = robinhood
        state["robinhood_nansen"] = robinhood_nansen
        state["execution_queue"] = queue
        state["multichain_wallets"] = merged
        state["selective_alpha"] = selective
        state["selective_db"] = selective_db
        state["telegram"] = telegram
        state["multichain"] = multichain
        state["last_run_status"] = "DONE" if multichain["status"] == "DONE" else "PARTIAL"
        state["last_run_finished_at"] = utc_now()
        store.save(state)
        store.log({"at": utc_now(), "event": "multichain_run_done", **multichain})
    except Exception:
        pass

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
