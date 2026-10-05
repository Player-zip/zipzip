from pathlib import Path
import json
import os
import shutil
import sys
import time
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from peixao import run_v6
from peixao.bootstrap import project_seed_ready, seed_project_if_needed
from peixao.config import settings
from peixao.execution_queue import build_execution_queue
from peixao.priority_validation import run_priority_validation_cycle
from peixao.robinhood_stream import (
    materialize_stream_candidates,
    serve_stream_receiver,
    stream_needs_materialization,
)
from peixao.stream_discovery import run_discovery_cycle_stream_first
from peixao.telegram_auth import process_auth_updates
from peixao.telegram_notifier import send_simulation_alert


def log(event: str, **fields):
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def _enabled(name: str, default: str = "1") -> bool:
    return str(os.getenv(name, default)).strip().lower() not in {"0", "false", "no", "off"}


def publish_completed_snapshot():
    """Atomically publish the last fully completed multichain score table."""
    source = settings.output_dir / "V22S_wallet_stage1.csv"
    target = settings.output_dir / "V22S_wallet_stage1_completed.csv"
    if not source.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    shutil.copyfile(source, tmp)
    os.replace(tmp, target)


def telegram_auth_loop():
    log("telegram_auth_listener_started")
    while True:
        try:
            auth = process_auth_updates(
                token=settings.telegram_bot_token,
                access_password=settings.telegram_access_password,
                db_path=settings.data_dir / "peixao_master.sqlite3",
                timeout=settings.telegram_timeout,
            )
            if auth.get("status") == "NOT_CONFIGURED":
                log("telegram_auth_not_configured")
            elif auth.get("processed", 0):
                log("telegram_auth", **auth)
        except Exception as exc:
            log("telegram_auth_error", error=f"{type(exc).__name__}: {exc}")
        time.sleep(3)


def quicknode_stream_receiver_loop():
    state_path = settings.state_dir / "robinhood_quicknode_stream.json"
    port = int(os.getenv("PEIXAO_HTTP_PORT", os.getenv("PORT", "8080")))
    log("robinhood_stream_receiver_started", port=port, path="/quicknode/robinhood-stream")
    try:
        serve_stream_receiver(
            state_path,
            port=port,
            security_token=os.getenv("QUICKNODE_STREAM_SECURITY_TOKEN"),
            signature_max_age_seconds=int(os.getenv("PEIXAO_STREAM_SIGNATURE_MAX_AGE_SECONDS", "600")),
        )
    except Exception as exc:
        log("robinhood_stream_receiver_error", error=f"{type(exc).__name__}: {exc}")


def alpha_simulation_once():
    if str(os.getenv("PEIXAO_ALPHA_SIMULATION_ONCE", "")).strip().lower() not in {"1", "true", "yes", "y"}:
        return
    run_id = str(os.getenv("PEIXAO_ALPHA_SIMULATION_RUN", "1")).strip() or "1"
    safe_run_id = "".join(ch for ch in run_id if ch.isalnum() or ch in {"-", "_"}) or "1"
    marker = settings.data_dir / f"alpha_simulation_once_{safe_run_id}.json"
    if marker.is_file():
        log("alpha_simulation_skipped", reason="ALREADY_SENT", run_id=run_id)
        return
    try:
        result = send_simulation_alert(
            token=settings.telegram_bot_token,
            db_path=settings.data_dir / "peixao_master.sqlite3",
            address="0x98c43da65205b7c53a857d79f3d1f6fbc0b76fa5",
            chain="Base",
            label="onchainunc.base.eth",
            pnl="+$6.55K",
            pnl_pct="+317%",
            win_rate="57.14%",
            buys=8,
            sells=7,
            buy_volume="$1.59K",
            sell_volume="$2.04K",
            profile="microcaps / multichain",
            alpha_score=None,
            tier=None,
            positions_per_week=None,
            timeout=settings.telegram_timeout,
        )
        log("alpha_simulation", run_id=run_id, **result)
        if result.get("sent", 0) > 0:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"sent": True, "run_id": run_id}), encoding="utf-8")
    except Exception as exc:
        log("alpha_simulation_error", run_id=run_id, error=f"{type(exc).__name__}: {exc}")


def ensure_seed(wait_missing: int) -> bool:
    if project_seed_ready(settings):
        return True
    try:
        bootstrap = seed_project_if_needed(settings)
        log("bootstrap_status", **bootstrap)
    except Exception as exc:
        log("bootstrap_error", error=f"{type(exc).__name__}: {exc}")
        time.sleep(wait_missing)
        return False
    if not project_seed_ready(settings):
        time.sleep(wait_missing)
        return False
    return True


if __name__ == "__main__":
    discovery_interval = max(300, int(os.getenv("PEIXAO_DISCOVERY_INTERVAL_SECONDS", "1800")))
    validation_interval = max(900, int(os.getenv("PEIXAO_VALIDATION_INTERVAL_SECONDS", "3600")))
    legacy_full_default = os.getenv("PEIXAO_WORKER_INTERVAL_SECONDS", "21600")
    full_validation_interval = max(
        3600,
        int(os.getenv("PEIXAO_FULL_VALIDATION_INTERVAL_SECONDS", legacy_full_default)),
    )
    scheduler_tick = max(10, int(os.getenv("PEIXAO_SCHEDULER_TICK_SECONDS", "30")))
    wait_missing = max(30, int(os.getenv("PEIXAO_WAIT_FOR_DATA_SECONDS", "60")))
    stream_enabled = _enabled("PEIXAO_ROBINHOOD_STREAM_ENABLED", "1")

    threading.Thread(target=telegram_auth_loop, name="telegram-auth", daemon=True).start()
    threading.Thread(target=alpha_simulation_once, name="alpha-simulation", daemon=True).start()
    if stream_enabled:
        threading.Thread(target=quicknode_stream_receiver_loop, name="robinhood-stream", daemon=True).start()

    completed_snapshot = settings.output_dir / "V22S_wallet_stage1_completed.csv"
    source_snapshot = settings.output_dir / "V22S_wallet_stage1.csv"
    has_baseline = completed_snapshot.is_file() or source_snapshot.is_file()

    log(
        "worker_started",
        discovery_interval_seconds=discovery_interval,
        validation_interval_seconds=validation_interval,
        full_validation_interval_seconds=full_validation_interval,
        scheduler_tick_seconds=scheduler_tick,
        robinhood_stream_enabled=stream_enabled,
        has_completed_baseline=has_baseline,
        project_root=str(settings.project_root),
        v3_cache_dir=str(settings.v3_cache_dir),
    )

    publish_completed_snapshot()

    last_discovery = 0.0
    last_validation = 0.0
    last_stream_flush = 0.0
    # A persisted completed score table means a heavy legacy refresh already
    # exists. Do not replay the legacy pipeline merely because Railway restarted.
    last_full_validation = time.monotonic() if has_baseline else 0.0
    stream_state_path = settings.state_dir / "robinhood_quicknode_stream.json"

    while True:
        if not ensure_seed(wait_missing):
            continue

        now = time.monotonic()

        # Stream deliveries are acknowledged immediately by the HTTP thread.
        # Materialization and the queue rebuild happen here so webhook latency
        # stays low and new cross-token wallets reach the execution queue within
        # one scheduler tick instead of waiting for the 30-minute radar.
        if (
            stream_enabled
            and now - last_stream_flush >= scheduler_tick
            and stream_needs_materialization(stream_state_path)
        ):
            started = time.time()
            try:
                candidates = materialize_stream_candidates(
                    stream_state_path,
                    settings.output_dir / "V22_robinhood_stream_wallet_candidates.csv",
                    rpc_url=os.getenv("ROBINHOOD_RPC_URL"),
                    timeout=max(settings.rpc_timeout, 15.0),
                    min_cross_token_hits=settings.birdeye_min_cross_token_hits,
                    max_wallets=int(os.getenv("PEIXAO_ROBINHOOD_MAX_WALLETS", "100")),
                    eoa_checks_per_cycle=int(os.getenv("PEIXAO_ROBINHOOD_STREAM_EOA_CHECKS", "20")),
                    max_rps=float(os.getenv("PEIXAO_ROBINHOOD_MAX_RPS", "15")),
                )
                queue = build_execution_queue(
                    settings.output_dir,
                    settings.state_dir,
                    stale_seconds=int(os.getenv("PEIXAO_EXECUTION_QUEUE_TTL_SECONDS", str(7 * 86400))),
                )
                log(
                    "robinhood_stream_flush_done",
                    elapsed_s=round(time.time() - started, 2),
                    candidates=candidates,
                    queue=queue,
                )
            except Exception as exc:
                log("robinhood_stream_flush_error", error=f"{type(exc).__name__}: {exc}")
            last_stream_flush = time.monotonic()
            continue

        # 1) Frequent discovery: QuickNode Stream is Robinhood primary when
        # configured; dRPC/Alchemy remain fallback and 6h reconciliation paths.
        if last_discovery == 0.0 or now - last_discovery >= discovery_interval:
            started = time.time()
            try:
                result = run_discovery_cycle_stream_first()
                log("discovery_cycle_done", elapsed_s=round(time.time() - started, 2), result=result)
            except Exception as exc:
                log("discovery_cycle_error", elapsed_s=round(time.time() - started, 2), error=f"{type(exc).__name__}: {exc}")
            last_discovery = time.monotonic()
            continue

        # 2) Hourly validation consumes the persistent priority queue only.
        if last_validation == 0.0 or now - last_validation >= validation_interval:
            started = time.time()
            try:
                result = run_priority_validation_cycle()
                publish_completed_snapshot()
                log("priority_validation_cycle_done", elapsed_s=round(time.time() - started, 2), result=result)
            except Exception as exc:
                log("priority_validation_cycle_error", elapsed_s=round(time.time() - started, 2), error=f"{type(exc).__name__}: {exc}")
            last_validation = time.monotonic()
            continue

        # 3) Heavy legacy refresh remains the RPC reconciliation/backfill path.
        if last_full_validation == 0.0 or now - last_full_validation >= full_validation_interval:
            started = time.time()
            try:
                result = run_v6()
                publish_completed_snapshot()
                log(
                    "validation_cycle_done",
                    mode="FULL_LEGACY_REFRESH",
                    elapsed_s=round(time.time() - started, 2),
                    result=result,
                )
            except Exception as exc:
                log(
                    "validation_cycle_error",
                    mode="FULL_LEGACY_REFRESH",
                    elapsed_s=round(time.time() - started, 2),
                    error=f"{type(exc).__name__}: {exc}",
                )
            last_full_validation = time.monotonic()
            continue

        time.sleep(scheduler_tick)
