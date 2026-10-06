from __future__ import annotations

from .config import Settings, cost_int, env_bool, env_float, env_int, settings
from .execution_queue import build_execution_queue
from .multichain_runner import _base_discovery, _quicknode_discovery, _robinhood_discovery, _solana_token_radar
from .robinhood_incremental import run_robinhood_radar_incremental
from .robinhood_stream import run_robinhood_stream_cycle
from .state import persist_summary, safe_call, utc_now


def _stream_enabled() -> bool:
    return env_bool("PEIXAO_ROBINHOOD_STREAM_ENABLED", True)


def _is_quicknode_endpoint(url: str | None) -> bool:
    value = str(url or "").strip().lower()
    return "quiknode.pro" in value or "quicknode.com" in value


def _rpc_scan_fallback(cfg: Settings, *, ttl_seconds: int) -> dict:
    """Use Alchemy before public RPC when the configured Robinhood endpoint is
    a QuickNode Discover endpoint.

    QuickNode remains the preferred lightweight RPC for Stream EOA checks, but
    the free Discover plan is intentionally not used for historical eth_getLogs
    scanning because its log range is too small for our discovery workload.
    """
    primary = str(cfg.robinhood_rpc_url or "").strip()
    if primary and not _is_quicknode_endpoint(primary):
        return _robinhood_discovery(cfg, ttl_seconds=ttl_seconds)

    return run_robinhood_radar_incremental(
        cfg.output_dir,
        cfg.state_dir,
        robinhood_rpc_url=None,
        public_rpc_url=cfg.rpc_url,
        alchemy_api_key=cfg.alchemy_api_key,
        timeout=max(cfg.rpc_timeout, 15.0),
        ttl_seconds=ttl_seconds,
        max_candidates=env_int("PEIXAO_ROBINHOOD_MAX_TOKENS", 40),
        max_shortlist=env_int("PEIXAO_ROBINHOOD_MAX_SHORTLIST", cfg.token_radar_max_shortlist),
        min_cross_token_hits=cfg.birdeye_min_cross_token_hits,
        max_wallets=env_int("PEIXAO_ROBINHOOD_MAX_WALLETS", 100),
        # Janela curta própria do fallback rápido; a de 6h usa
        # PEIXAO_ROBINHOOD_LOOKBACK_BLOCKS / PEIXAO_ROBINHOOD_LOG_CHUNK_BLOCKS.
        lookback_blocks=env_int("PEIXAO_ROBINHOOD_FAST_LOOKBACK_BLOCKS", 30000),
        chunk_blocks=env_int("PEIXAO_ROBINHOOD_FAST_LOG_CHUNK_BLOCKS", 9500),
        max_logs_per_token=env_int("PEIXAO_ROBINHOOD_MAX_LOGS_PER_TOKEN", 1200),
        max_rps=env_float("PEIXAO_ROBINHOOD_MAX_RPS", 15.0),
        prefer_free_rpc=env_bool("PEIXAO_ROBINHOOD_PREFER_FREE_RPC", bool(cost_int("PEIXAO_ROBINHOOD_PREFER_FREE_RPC"))),
    )


def _stream_first_robinhood(cfg: Settings, *, ttl_seconds: int) -> dict:
    if not cfg.chain_enabled("robinhood"):
        return {"status": "CHAIN_DISABLED", "chain": "robinhood", "http_calls": 0}
    if not _stream_enabled():
        return _rpc_scan_fallback(cfg, ttl_seconds=ttl_seconds)

    stream = run_robinhood_stream_cycle(
        cfg.output_dir,
        cfg.state_dir,
        rpc_url=cfg.robinhood_eoa_rpc_url,
        api_key=cfg.quicknode_streams_api_key,
        webhook_url=cfg.robinhood_stream_webhook_url,
        timeout=max(cfg.rpc_timeout, 15.0),
        ttl_seconds=ttl_seconds,
        max_candidates=env_int("PEIXAO_ROBINHOOD_MAX_TOKENS", 40),
        max_shortlist=env_int("PEIXAO_ROBINHOOD_MAX_SHORTLIST", cfg.token_radar_max_shortlist),
        min_cross_token_hits=cfg.birdeye_min_cross_token_hits,
        max_wallets=env_int("PEIXAO_ROBINHOOD_MAX_WALLETS", 100),
        eoa_checks_per_cycle=env_int("PEIXAO_ROBINHOOD_STREAM_EOA_CHECKS", 20),
        max_rps=env_float("PEIXAO_ROBINHOOD_MAX_RPS", 15.0),
        rpc_backfill_interval_seconds=env_int("PEIXAO_ROBINHOOD_RPC_BACKFILL_INTERVAL_SECONDS", 21600),
    )
    if stream.get("stream_active"):
        # The heavy 6-hour run remains the reconciliation/backfill path.
        # The frequent discovery loop does not poll historical logs while the
        # Stream is healthy.
        return stream

    fallback = _rpc_scan_fallback(cfg, ttl_seconds=ttl_seconds)
    return {
        **fallback,
        "stream": stream.get("stream"),
        "stream_candidates": stream.get("stream_candidates"),
        "stream_active": False,
        "stream_fallback": True,
    }


def run_discovery_cycle_stream_first(*, cfg: Settings = settings) -> dict:
    """30-minute discovery with QuickNode Stream as Robinhood primary source."""
    cfg.ensure_dirs()
    fast_ttl = max(300, env_int("PEIXAO_FAST_RADAR_TTL_SECONDS", 600))

    # Bring the Robinhood Stream up first. This makes stream availability
    # independent of slower Solana holder refreshes and starts receiving events
    # as early as possible after a Railway restart.
    robinhood = safe_call("fast_robinhood_stream", lambda: _stream_first_robinhood(cfg, ttl_seconds=fast_ttl))
    solana_radar = safe_call("fast_solana_radar", lambda: _solana_token_radar(cfg, ttl_seconds=fast_ttl))
    # QuickNode Solana gasta créditos: no perfil economy roda só no ciclo de 6h.
    if env_bool("PEIXAO_QUICKNODE_FAST_CYCLE", bool(cost_int("PEIXAO_QUICKNODE_FAST_CYCLE"))):
        solana_wallets = safe_call("fast_quicknode_solana", lambda: _quicknode_discovery(cfg))
    else:
        solana_wallets = {"status": "SKIPPED_COST_PROFILE", "quicknode_credits": 0}
    base = safe_call("fast_base_radar", lambda: _base_discovery(cfg, ttl_seconds=fast_ttl))
    queue = safe_call("fast_execution_queue", lambda: build_execution_queue(
        cfg.output_dir,
        cfg.state_dir,
        stale_seconds=env_int("PEIXAO_EXECUTION_QUEUE_TTL_SECONDS", 7 * 86400),
    ))

    summary = {
        "status": "DONE" if all(x.get("status") != "ERROR" for x in (solana_radar, solana_wallets, base, robinhood, queue)) else "PARTIAL",
        "mode": "STREAM_FIRST",
        "solana_radar": solana_radar,
        "solana_wallets": solana_wallets,
        "base": base,
        "robinhood": robinhood,
        "execution_queue": queue,
        "finished_at": utc_now(),
    }
    persist_summary(
        cfg.state_dir,
        {
            "fast_discovery": summary,
            "fast_discovery_finished_at": summary["finished_at"],
            "robinhood_stream_first": bool(robinhood.get("stream_active")),
        },
        {"at": utc_now(), "event": "fast_discovery_done", **summary},
    )
    return summary
