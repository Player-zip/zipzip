from __future__ import annotations

import time

from .alpha_db import record_alpha22_stage1_csv
from .alpha_v22 import build_alpha_v22_stage1
from .birdeye_alpha import run_birdeye_alpha_discovery
from .config import settings, Settings
from .dune import probe_dune
from .dune_selectivity import enrich_stage1_with_dune
from .gmgn_live import probe_gmgn_live
from .jupiter import probe_jupiter
from .mobula import probe_mobula
from .rpc_budget import RpcBudgetManager
from .quicknode_solana import merge_quicknode_with_birdeye, quicknode_solana_enabled, run_quicknode_solana_from_settings
from .solana_tracker import probe_solana_tracker
from .selective_alpha import build_selective_stage1
from .state import PipelineState, logger, utc_now
from .token_radar import run_token_radar
from .telegram_notifier import send_test_alert_once
from .wallet_db import record_ranked_v1_csv
from .pipeline import (
    discover_offline,
    resolve_tx_origins,
    classify_candidates,
    seed_cache_if_missing,
    build_wallet_queue,
    apply_cached_gmgn,
    validate_against_v5,
)


def run_v6(*, cfg: Settings = settings, validate_against_reference: bool = True, top_ready: int = 100) -> dict:
    """Run validated V6 plus parallel, failure-isolated Selective Alpha V2.2S1."""
    cfg.ensure_dirs()
    cfg.validate_legacy_inputs()
    assert cfg.project_root is not None and cfg.v3_cache_dir is not None

    rpc_budget = RpcBudgetManager(
        cfg.data_dir / "rpc_budget.sqlite3",
        enabled=cfg.rpc_budget_enabled,
        run_limit=cfg.rpc_budget_run_total,
        hourly_limit=cfg.rpc_budget_hourly_total,
        daily_limit=cfg.rpc_budget_daily_total,
        stage_limits=cfg.rpc_stage_budgets,
    )

    state_store = PipelineState(cfg.state_dir)
    state = state_store.load()
    state["runs"] = int(state.get("runs", 0)) + 1
    state["last_run_started_at"] = utc_now()
    state["last_run_status"] = "RUNNING"
    state_store.save(state)

    seed_cache_if_missing(
        cfg.v5_reference_dir,
        cfg.cache_dir,
        ("tx_origin_cache.json", "address_code_cache.json"),
    )

    def stage(name, fn):
        started = time.time()
        state.setdefault("stages", {})[name] = {"status": "RUNNING", "started_at": utc_now()}
        state_store.save(state)
        try:
            result = fn()
        except Exception as exc:
            state["stages"][name] = {
                "status": "ERROR",
                "finished_at": utc_now(),
                "elapsed_s": round(time.time() - started, 2),
                "error": f"{type(exc).__name__}: {exc}",
            }
            state["last_run_status"] = "ERROR"
            state["last_error_stage"] = name
            state_store.save(state)
            state_store.log({"at": utc_now(), "event": "stage_error", "stage": name, "error": str(exc)})
            raise
        state["stages"][name] = {"status": "DONE", "finished_at": utc_now(), "elapsed_s": round(time.time() - started, 2)}
        state_store.save(state)
        return result

    def optional_stage(name, fn):
        """New providers must never take down the validated V6 worker."""
        started = time.time()
        state.setdefault("stages", {})[name] = {"status": "RUNNING", "started_at": utc_now()}
        state_store.save(state)
        try:
            result = fn()
            status = "ERROR" if isinstance(result, dict) and result.get("status") == "ERROR" else "DONE"
        except Exception as exc:
            logger.exception("etapa opcional %s falhou", name)
            result = {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
            status = "ERROR"
            state_store.log({"at": utc_now(), "event": "optional_stage_error", "stage": name, "error": str(exc)})
        state["stages"][name] = {"status": status, "finished_at": utc_now(), "elapsed_s": round(time.time() - started, 2)}
        state_store.save(state)
        return result

    discovery = stage("02_offline_discovery", lambda: discover_offline(
        cfg.project_root, cfg.v3_cache_dir, cfg.output_dir,
        start_checkpoint=cfg.start_checkpoint, end_checkpoint=cfg.end_checkpoint,
        min_tokens=cfg.min_tokens, tx_per_token=cfg.tx_per_token,
    ))

    origins = stage("03_tx_origin_rpc", lambda: resolve_tx_origins(
        discovery["tx_sample"], cfg.cache_dir, cfg.output_dir,
        rpc_url=cfg.rpc_url, min_tokens=cfg.min_tokens, max_rpc_tx=cfg.max_rpc_tx,
        timeout=cfg.rpc_timeout, retries=cfg.rpc_retries, delay=cfg.rpc_delay,
        run_rpc=cfg.run_rpc, rpc_budget=rpc_budget, rpc_provider="primary_evm",
    ))

    classified = stage("04_classification", lambda: classify_candidates(
        origins["origin_candidates"], discovery["actor_candidates"], cfg.cache_dir, cfg.output_dir,
        rpc_url=cfg.rpc_url, timeout=cfg.rpc_timeout, retries=cfg.rpc_retries,
        delay=cfg.rpc_delay, run_rpc=cfg.run_rpc, rpc_budget=rpc_budget, rpc_provider="primary_evm",
    ))

    queue, queue_summary = stage("05_queue_score", lambda: build_wallet_queue(
        classified["final_candidates"], cfg.output_dir, top_ready=top_ready,
    ))

    gmgn_summary = stage("06_cached_gmgn_score", lambda: apply_cached_gmgn(queue, cfg.gmgn_cache, cfg.output_dir))

    alpha_v22_summary = stage("06b_alpha_v22_stage1", lambda: build_alpha_v22_stage1(
        cfg.output_dir / "V6_wallet_queue_enriched.csv", cfg.output_dir, max_deep_dive=30,
    ))

    master_db_summary = stage("07_master_wallet_db", lambda: record_ranked_v1_csv(
        cfg.output_dir / "V6_peixao_ranked_v1.csv", cfg.data_dir / "peixao_master.sqlite3",
    ))

    alpha_v22_db_summary = stage("07b_alpha_v22_db", lambda: record_alpha22_stage1_csv(
        cfg.output_dir / "V22_wallet_stage1.csv", cfg.data_dir / "peixao_master.sqlite3",
    ))

    if cfg.gmgn_live_probe:
        gmgn_live_summary = optional_stage("08_gmgn_live_probe", lambda: probe_gmgn_live(
            cfg.output_dir, cfg.state_dir, api_key=cfg.gmgn_api_key,
            demo_enabled=cfg.gmgn_demo_enabled, chain=cfg.gmgn_chain, timeout=cfg.gmgn_live_timeout,
        ))
    else:
        gmgn_live_summary = {"status": "DISABLED"}

    if cfg.mobula_probe_enabled:
        mobula_summary = optional_stage("08b_mobula_probe", lambda: probe_mobula(
            cfg.state_dir, api_key=cfg.mobula_api_key, base_url=cfg.mobula_base_url,
            timeout=cfg.mobula_timeout, ttl_seconds=cfg.mobula_probe_ttl_seconds,
        ))
    else:
        mobula_summary = {"status": "DISABLED", "cached": False, "http_calls": 0}

    if cfg.jupiter_probe_enabled:
        jupiter_summary = optional_stage("08c_jupiter_probe", lambda: probe_jupiter(
            cfg.state_dir, api_key=cfg.jupiter_api_key, base_url=cfg.jupiter_base_url,
            timeout=cfg.jupiter_timeout, ttl_seconds=cfg.jupiter_probe_ttl_seconds,
        ))
    else:
        jupiter_summary = {"status": "DISABLED", "cached": False, "http_calls": 0}

    if cfg.solana_tracker_probe_enabled:
        solana_tracker_summary = optional_stage("08d_solana_tracker_probe", lambda: probe_solana_tracker(
            cfg.state_dir, api_key=cfg.solana_tracker_api_key, base_url=cfg.solana_tracker_base_url,
            timeout=cfg.solana_tracker_timeout, ttl_seconds=cfg.solana_tracker_probe_ttl_seconds,
        ))
    else:
        solana_tracker_summary = {"status": "DISABLED", "cached": False, "http_calls": 0}

    if cfg.dune_probe_enabled:
        dune_summary = optional_stage("08e_dune_probe", lambda: probe_dune(
            cfg.state_dir, api_key=cfg.dune_api_key, base_url=cfg.dune_base_url,
            timeout=cfg.dune_timeout, ttl_seconds=cfg.dune_probe_ttl_seconds,
        ))
    else:
        dune_summary = {"status": "DISABLED", "cached": False, "http_calls": 0}

    # --- Selective Alpha V2.2S1: isolated from frozen V5/V6/V0/V1/V2.2 outputs ---
    if cfg.token_radar_enabled:
        radar_summary = optional_stage("09_token_radar", lambda: run_token_radar(
            cfg.output_dir, cfg.state_dir, cfg.data_dir / "peixao_master.sqlite3",
            jupiter_api_key=cfg.jupiter_api_key, jupiter_base_url=cfg.jupiter_base_url,
            dexscreener_base_url=cfg.dexscreener_base_url, timeout=cfg.jupiter_timeout,
            ttl_seconds=cfg.token_radar_ttl_seconds, max_candidates=cfg.token_radar_max_candidates,
            max_shortlist=cfg.token_radar_max_shortlist,
            min_liquidity_usd=cfg.token_radar_min_liquidity_usd,
            min_volume_24h_usd=cfg.token_radar_min_volume_24h_usd,
            min_radar_score=cfg.token_radar_min_score,
        ))
    else:
        radar_summary = {"status": "DISABLED", "http_calls": 0, "rpc_calls": 0}

    quicknode_enabled = quicknode_solana_enabled()
    if quicknode_enabled:
        quicknode_summary = optional_stage("09b_quicknode_solana", lambda: run_quicknode_solana_from_settings(cfg))
    else:
        quicknode_summary = {"status": "DISABLED", "rpc_calls": 0, "quicknode_credits": 0}

    if cfg.birdeye_alpha_enabled:
        birdeye_alpha_summary = optional_stage("10_birdeye_alpha_discovery", lambda: run_birdeye_alpha_discovery(
            cfg.output_dir / "V22_token_radar_shortlist.csv", cfg.output_dir, cfg.state_dir,
            api_key=cfg.birdeye_api_key, base_url=cfg.birdeye_base_url,
            timeout=cfg.birdeye_alpha_timeout, delay=cfg.birdeye_alpha_delay,
            max_tokens=cfg.birdeye_alpha_max_tokens, top_traders_per_token=cfg.birdeye_top_traders_per_token,
            min_cross_token_hits=cfg.birdeye_min_cross_token_hits, max_pnl_wallets=cfg.birdeye_max_pnl_wallets,
            top_trader_ttl_seconds=cfg.birdeye_top_trader_ttl_seconds, pnl_ttl_seconds=cfg.birdeye_pnl_ttl_seconds,
        ))
    else:
        birdeye_alpha_summary = {"status": "DISABLED", "http_calls": 0}

    quicknode_merged_path = cfg.output_dir / "V22_radar_wallet_enriched_quicknode.csv"
    if quicknode_enabled:
        quicknode_merge_summary = optional_stage("10b_quicknode_birdeye_merge", lambda: merge_quicknode_with_birdeye(
            cfg.output_dir / "V22_radar_wallet_enriched.csv",
            cfg.output_dir / "V22_quicknode_wallet_candidates.csv",
            quicknode_merged_path,
        ))
    else:
        quicknode_merge_summary = {"status": "DISABLED"}
    solana_wallet_input = quicknode_merged_path if quicknode_merged_path.is_file() else cfg.output_dir / "V22_radar_wallet_enriched.csv"

    selective_pre_summary = optional_stage("11_selective_stage1_pre_dune", lambda: build_selective_stage1(
        cfg.output_dir / "V6_wallet_queue_enriched.csv",
        solana_wallet_input,
        cfg.output_dir,
        artifact_prefix="V22S_pre_dune",
        max_deep_dive=30,
    ))

    dune_selective_summary = optional_stage("12_dune_selectivity_validation", lambda: enrich_stage1_with_dune(
        cfg.output_dir / "V22S_pre_dune_wallet_stage1.csv", cfg.output_dir, cfg.state_dir,
        api_key=cfg.dune_api_key, enabled=cfg.dune_selectivity_enabled,
        base_url=cfg.dune_base_url.rstrip("/") + "/api/v1", timeout=max(cfg.dune_timeout, 15.0),
        ttl_seconds=cfg.dune_selectivity_ttl_seconds, max_wallets=cfg.dune_selectivity_max_wallets,
        lookback_days=cfg.dune_selectivity_lookback_days, max_poll_seconds=cfg.dune_selectivity_poll_seconds,
    ))

    # A tabela final (V22S_wallet_stage1.csv), o registro no banco e os alertas
    # saem de final_stage.build_final_stage1, chamado pelo multichain_runner e
    # pelo ciclo horário. Este runner só produz insumos (incluindo o cache Dune).

    telegram_test_summary = optional_stage("15a_telegram_test_once", lambda: send_test_alert_once(
        cfg.state_dir, token=cfg.telegram_bot_token, chat_id=cfg.telegram_chat_id,
        enabled=cfg.telegram_test_once, timeout=cfg.telegram_timeout,
    ))

    validation = validate_against_v5(cfg.v5_reference_dir, cfg.output_dir) if validate_against_reference else []
    rpc_budget_summary = rpc_budget.summary()
    summaries = {
        "validation": validation,
        "queue": queue_summary,
        "gmgn": gmgn_summary,
        "alpha_v22": alpha_v22_summary,
        "alpha_v22_db": alpha_v22_db_summary,
        "gmgn_live": gmgn_live_summary,
        "mobula": mobula_summary,
        "jupiter": jupiter_summary,
        "dune": dune_summary,
        "solana_tracker": solana_tracker_summary,
        "radar": radar_summary,
        "quicknode_solana": quicknode_summary,
        "birdeye_alpha": birdeye_alpha_summary,
        "quicknode_birdeye_merge": quicknode_merge_summary,
        "selective_pre_dune": selective_pre_summary,
        "dune_selectivity": dune_selective_summary,
        "telegram_test": telegram_test_summary,
        "master_wallet_db": master_db_summary,
        "rpc_budget": rpc_budget_summary,
    }
    state.update(summaries)
    state["last_run_status"] = "DONE"
    state["last_run_finished_at"] = utc_now()
    state_store.save(state)
    state_store.log({"at": utc_now(), "event": "run_done", **summaries})
    return {"status": "DONE", "output_dir": str(cfg.output_dir), **summaries}


def run(tokens: list[str] | None = None, wallets: list[str] | None = None, *, cfg: Settings = settings) -> dict:
    if tokens or wallets:
        return {
            "status": "QUEUED_INPUT_NOT_YET_BOUND_TO_DISCOVERY",
            "tokens": tokens or [],
            "wallets": wallets or [],
            "message": "A fila de comandos será conectada ao worker/provider layer na próxima etapa.",
        }
    return run_v6(cfg=cfg)
