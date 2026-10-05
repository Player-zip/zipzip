from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import os
import shutil
import subprocess

import pandas as pd


GMGN_DEMO_KEY = "gmgn_solbscbaseethmonadtron"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _find_cli() -> str | None:
    global_cli = shutil.which("gmgn-cli")
    if global_cli:
        return global_cli
    root = Path(__file__).resolve().parents[2]
    local_cli = root / "node_modules" / ".bin" / "gmgn-cli"
    return str(local_cli) if local_cli.is_file() else None


def _pick_probe_wallet(output_dir: Path) -> str | None:
    candidates = (
        output_dir / "V6_deep_dive_queue.csv",
        output_dir / "V6_peixao_ranked_v1.csv",
        output_dir / "V6_wallet_queue_enriched.csv",
    )
    for path in candidates:
        if not path.is_file():
            continue
        try:
            frame = pd.read_csv(path)
        except Exception:
            continue
        if "address" not in frame.columns:
            continue
        addresses = frame["address"].dropna().astype(str).str.strip()
        addresses = addresses[addresses.ne("")]
        if len(addresses):
            return addresses.iloc[0].lower()
    return None


def _run_read_only(cli: str, args: list[str], api_key: str, timeout: int) -> dict:
    env = os.environ.copy()
    env["GMGN_API_KEY"] = api_key
    cmd = [cli] + args + ["--raw"]
    try:
        proc = subprocess.run(
            cmd,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "TIMEOUT", "returncode": None, "data": None}
    except Exception as exc:
        return {"status": "ERROR", "returncode": None, "data": None, "error": f"{type(exc).__name__}: {exc}"}

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    data = None
    if stdout:
        try:
            data = json.loads(stdout)
        except Exception:
            pass

    if proc.returncode == 0 and data is not None:
        status = "OK"
    elif proc.returncode in {401, 403} or "401" in stderr or "403" in stderr:
        status = "AUTH_ERROR"
    elif proc.returncode == 0:
        status = "NON_JSON"
    else:
        status = "ERROR"

    result = {
        "status": status,
        "returncode": proc.returncode,
        "data": data,
    }
    if status != "OK":
        # Keep diagnostics useful without logging secrets or massive responses.
        result["stderr"] = stderr[-1000:]
        result["stdout_tail"] = stdout[-1000:]
    return result


def _safe_summary(payload: dict) -> dict:
    results = payload.get("results") if isinstance(payload, dict) else None
    command_statuses = {}
    if isinstance(results, dict):
        command_statuses = {
            str(name): str(item.get("status", "UNKNOWN")) if isinstance(item, dict) else "UNKNOWN"
            for name, item in results.items()
        }
    return {
        "previous_status": payload.get("status") if isinstance(payload, dict) else None,
        "mode": payload.get("mode") if isinstance(payload, dict) else None,
        "chain": payload.get("chain") if isinstance(payload, dict) else None,
        "wallet": payload.get("wallet") if isinstance(payload, dict) else None,
        "commands_ok": payload.get("commands_ok") if isinstance(payload, dict) else None,
        "commands_total": payload.get("commands_total") if isinstance(payload, dict) else None,
        "command_statuses": command_statuses,
        "checked_at": payload.get("checked_at") if isinstance(payload, dict) else None,
    }


def probe_gmgn_live(
    output_dir: Path,
    state_dir: Path,
    *,
    api_key: str | None,
    demo_enabled: bool,
    chain: str,
    timeout: int = 45,
) -> dict:
    """Run a small read-only GMGN compatibility probe.

    If no personal key exists, the public demo key is used exactly once and a
    marker is stored on the persistent volume. This prevents a test credential
    from silently becoming a recurring production provider.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    personal_key = (api_key or "").strip()
    mode = "PERSONAL" if personal_key else "DEMO_TEST"
    marker = state_dir / "gmgn_demo_probe_done.json"
    probe_path = output_dir / "V6_gmgn_live_probe.json"

    if not personal_key:
        if not demo_enabled:
            return {"status": "DISABLED_NO_PERSONAL_KEY", "mode": "NONE"}
        if marker.is_file():
            summary = {}
            if probe_path.is_file():
                try:
                    payload = json.loads(probe_path.read_text(encoding="utf-8"))
                    summary = _safe_summary(payload)
                except Exception:
                    summary = {}
            return {
                "status": "SKIPPED_DEMO_ALREADY_PROBED",
                "mode": mode,
                "marker": str(marker),
                "output": str(probe_path),
                **summary,
            }
        effective_key = GMGN_DEMO_KEY
    else:
        effective_key = personal_key

    wallet = _pick_probe_wallet(output_dir)
    if not wallet:
        return {"status": "SKIPPED_NO_WALLET", "mode": mode, "chain": chain}

    cli = _find_cli()
    if not cli:
        return {
            "status": "ERROR_CLI_NOT_FOUND",
            "mode": mode,
            "chain": chain,
            "wallet": wallet,
        }

    commands = {
        "stats": ["portfolio", "stats", "--chain", chain, "--wallet", wallet],
        "profits_30d": ["portfolio", "profits", "--chain", chain, "--wallet", wallet, "--period", "30d"],
        "holdings": ["portfolio", "holdings", "--chain", chain, "--wallet", wallet, "--limit", "20"],
        "activity": ["portfolio", "activity", "--chain", chain, "--wallet", wallet, "--limit", "20"],
    }

    results = {
        name: _run_read_only(cli, args, effective_key, timeout)
        for name, args in commands.items()
    }
    ok_count = sum(1 for item in results.values() if item.get("status") == "OK")
    if ok_count == len(results):
        status = "DONE"
    elif ok_count:
        status = "PARTIAL"
    else:
        status = "FAILED"

    payload = {
        "status": status,
        "mode": mode,
        "chain": chain,
        "wallet": wallet,
        "checked_at": _now(),
        "commands_ok": ok_count,
        "commands_total": len(results),
        "demo_test_only": mode == "DEMO_TEST",
        "results": results,
    }
    probe_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if mode == "DEMO_TEST":
        marker_payload = {
            "attempted_at": payload["checked_at"],
            "status": status,
            "commands_ok": ok_count,
            "commands_total": len(results),
            "wallet": wallet,
            "chain": chain,
        }
        marker.write_text(json.dumps(marker_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    command_statuses = {
        name: str(item.get("status", "UNKNOWN"))
        for name, item in results.items()
    }
    return {
        "status": status,
        "mode": mode,
        "chain": chain,
        "wallet": wallet,
        "commands_ok": ok_count,
        "commands_total": len(results),
        "command_statuses": command_statuses,
        "output": str(probe_path),
        "demo_test_only": mode == "DEMO_TEST",
    }
