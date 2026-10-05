from __future__ import annotations

import base64
from datetime import datetime, timezone
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import gzip
import hmac
import json
import math
import os
from pathlib import Path
import threading
import time

import pandas as pd
import requests

from .evm_radar import ZERO_ADDRESS, _address, run_robinhood_radar
from .robinhood_chainstack import ROBINHOOD_CHAIN_ID, _is_eoa_paced


TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
STREAM_API_BASE = "https://api.quicknode.com/streams/rest/v1/streams"
STREAM_NETWORK = "robinhood-mainnet"
STREAM_DATASET = "logs"
STREAM_WEBHOOK_PATH = "/quicknode/robinhood-stream"
_STATE_LOCK = threading.RLock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _normalize_evm_address(value) -> str:
    address = _address(str(value or ""))
    return str(address or "").lower()


def _topic_address(topic) -> str:
    value = str(topic or "").lower().removeprefix("0x")
    if len(value) < 40:
        return ""
    candidate = "0x" + value[-40:]
    return _normalize_evm_address(candidate)


def _hex_int(value) -> int | None:
    try:
        text = str(value or "")
        return int(text, 16) if text.lower().startswith("0x") else int(text)
    except Exception:
        return None


def _event_id(event: dict) -> str:
    token = _normalize_evm_address(event.get("token") or event.get("address"))
    tx = str(event.get("transaction_hash") or event.get("transactionHash") or "").lower()
    log_index = str(event.get("log_index") or event.get("logIndex") or "")
    block = str(event.get("block_number") or event.get("blockNumber") or "")
    return f"{token}:{tx}:{log_index}:{block}"


def _raw_log_event(log: dict) -> dict | None:
    topics = log.get("topics") if isinstance(log.get("topics"), list) else []
    if len(topics) < 3 or str(topics[0]).lower() != TRANSFER_TOPIC:
        return None
    token = _normalize_evm_address(log.get("address"))
    sender = _topic_address(topics[1])
    receiver = _topic_address(topics[2])
    if not token or not sender or not receiver:
        return None
    return {
        "token": token,
        "from": sender,
        "to": receiver,
        "block_number": log.get("blockNumber"),
        "transaction_hash": log.get("transactionHash"),
        "log_index": log.get("logIndex"),
        "removed": bool(log.get("removed", False)),
    }


def extract_stream_events(payload: dict) -> tuple[list[dict], dict]:
    """Accept the compact Raullux filter output and raw Logs dataset payloads."""
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    direct = payload.get("events")
    if isinstance(direct, list):
        return [x for x in direct if isinstance(x, dict)], metadata

    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("events"), list):
        return [x for x in data["events"] if isinstance(x, dict)], metadata

    items = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    events: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        parsed = _raw_log_event(item)
        if parsed:
            events.append(parsed)
    return events, metadata


def verify_stream_signature(
    body_text: str,
    headers,
    security_token: str | None,
    *,
    max_age_seconds: int = 600,
) -> tuple[bool, str]:
    token = str(security_token or "").strip()
    if not token:
        return False, "SECURITY_TOKEN_MISSING"
    nonce = str(headers.get("X-QN-Nonce") or headers.get("x-qn-nonce") or "")
    timestamp = str(headers.get("X-QN-Timestamp") or headers.get("x-qn-timestamp") or "")
    signature = str(headers.get("X-QN-Signature") or headers.get("x-qn-signature") or "").strip().lower()
    if not nonce or not timestamp or not signature:
        return False, "SIGNATURE_HEADERS_MISSING"
    try:
        ts = int(float(timestamp))
        if ts > 10_000_000_000:
            ts //= 1000
        if abs(int(time.time()) - ts) > max(60, int(max_age_seconds)):
            return False, "TIMESTAMP_OUT_OF_RANGE"
    except Exception:
        return False, "TIMESTAMP_INVALID"
    expected = hmac.new(
        token.encode("utf-8"),
        (nonce + timestamp + body_text).encode("utf-8"),
        sha256,
    ).hexdigest().lower()
    if signature.startswith("sha256="):
        signature = signature.split("=", 1)[1]
    return (True, "OK") if hmac.compare_digest(expected, signature) else (False, "SIGNATURE_INVALID")


def ingest_stream_payload(
    payload: dict,
    state_path: Path,
    *,
    wallet_ttl_seconds: int = 7 * 86400,
    dedupe_ttl_seconds: int = 2 * 86400,
) -> dict:
    events, metadata = extract_stream_events(payload)
    now = int(time.time())
    now_iso = _now_iso()
    with _STATE_LOCK:
        state = _load_json(state_path)
        wallets = state.get("wallets") if isinstance(state.get("wallets"), dict) else {}
        watchlist = set(state.get("watchlist") or [])
        seen = state.get("seen_events") if isinstance(state.get("seen_events"), dict) else {}
        seen_cutoff = now - max(3600, int(dedupe_ttl_seconds))
        seen = {k: int(v) for k, v in seen.items() if int(v or 0) >= seen_cutoff}

        accepted = duplicates = ignored = 0
        for event in events:
            token = _normalize_evm_address(event.get("token") or event.get("address"))
            if not token or (watchlist and token not in watchlist) or bool(event.get("removed", False)):
                ignored += 1
                continue
            event_id = _event_id(event)
            if event_id in seen:
                duplicates += 1
                continue
            seen[event_id] = now
            accepted += 1
            for raw_wallet in (event.get("from"), event.get("to")):
                wallet = _normalize_evm_address(raw_wallet)
                if not wallet or wallet == ZERO_ADDRESS or wallet == token or wallet in watchlist:
                    continue
                item = wallets.get(wallet) if isinstance(wallets.get(wallet), dict) else {}
                token_counts = item.get("token_counts") if isinstance(item.get("token_counts"), dict) else {}
                token_counts[token] = int(token_counts.get(token, 0) or 0) + 1
                wallets[wallet] = {
                    **item,
                    "token_counts": token_counts,
                    "total_events": int(item.get("total_events", 0) or 0) + 1,
                    "first_seen_epoch": int(item.get("first_seen_epoch", now) or now),
                    "first_seen_at": str(item.get("first_seen_at", now_iso) or now_iso),
                    "last_seen_epoch": now,
                    "last_seen_at": now_iso,
                }

        wallet_cutoff = now - max(3600, int(wallet_ttl_seconds))
        wallets = {
            wallet: item for wallet, item in wallets.items()
            if isinstance(item, dict) and int(item.get("last_seen_epoch", 0) or 0) >= wallet_cutoff
        }
        batch_end = metadata.get("batch_end_range")
        state.update({
            "updated_epoch": now,
            "updated_at": now_iso,
            "last_delivery_epoch": now,
            "last_delivery_at": now_iso,
            "last_delivery_batch_end": _hex_int(batch_end),
            "delivery_count": int(state.get("delivery_count", 0) or 0) + 1,
            "events_ingested": int(state.get("events_ingested", 0) or 0) + accepted,
            "wallets": wallets,
            "seen_events": seen,
        })
        _atomic_json(state_path, state)
    return {
        "status": "DONE",
        "events_received": len(events),
        "events_accepted": accepted,
        "duplicates": duplicates,
        "ignored": ignored,
        "wallets_tracked": len(wallets),
    }


def _filter_code(tokens: list[str]) -> str:
    watch = json.dumps(sorted(set(_normalize_evm_address(x) for x in tokens if _normalize_evm_address(x))))
    return f'''function main(stream) {{
  const WATCH = new Set({watch});
  const TRANSFER = "{TRANSFER_TOPIC}";
  const rows = Array.isArray(stream.data) ? stream.data : (stream.data ? [stream.data] : []);
  const events = [];
  for (const log of rows) {{
    if (!log || !Array.isArray(log.topics) || log.topics.length < 3) continue;
    const topic0 = String(log.topics[0] || "").toLowerCase();
    const token = String(log.address || "").toLowerCase();
    if (topic0 !== TRANSFER || !WATCH.has(token)) continue;
    const from = "0x" + String(log.topics[1] || "").replace(/^0x/, "").slice(-40).toLowerCase();
    const to = "0x" + String(log.topics[2] || "").replace(/^0x/, "").slice(-40).toLowerCase();
    events.push({{
      token, from, to,
      block_number: log.blockNumber,
      transaction_hash: log.transactionHash,
      log_index: log.logIndex,
      removed: Boolean(log.removed)
    }});
  }}
  if (!events.length) return null;
  return {{events, metadata: stream.metadata}};
}}'''


def _encoded_filter(tokens: list[str]) -> tuple[str, str]:
    code = _filter_code(tokens)
    return base64.b64encode(code.encode("utf-8")).decode("ascii"), sha256(code.encode("utf-8")).hexdigest()


def _stream_api(
    method: str,
    path: str,
    *,
    api_key: str,
    payload: dict | None = None,
    timeout: float = 15.0,
) -> tuple[int, dict]:
    response = requests.request(
        method,
        STREAM_API_BASE + path,
        headers={
            "accept": "application/json",
            "Content-Type": "application/json",
            "x-api-key": api_key,
        },
        json=payload,
        timeout=float(timeout),
    )
    try:
        body = response.json()
    except Exception:
        body = {}
    return int(response.status_code), body if isinstance(body, dict) else {}


def _resolve_webhook_url(explicit: str | None = None) -> str:
    value = str(explicit or "").strip()
    if value:
        return value.rstrip("/")
    domain = str(os.getenv("RAILWAY_PUBLIC_DOMAIN", "") or os.getenv("RAILWAY_STATIC_URL", "")).strip()
    if not domain:
        return ""
    if not domain.startswith("http://") and not domain.startswith("https://"):
        domain = "https://" + domain
    return domain.rstrip("/") + STREAM_WEBHOOK_PATH


def sync_robinhood_stream(
    shortlist_path: Path,
    state_path: Path,
    *,
    api_key: str | None,
    webhook_url: str | None = None,
    timeout: float = 15.0,
    batch_size: int = 20,
) -> dict:
    try:
        shortlist = pd.read_csv(shortlist_path) if shortlist_path.is_file() else pd.DataFrame()
    except Exception:
        shortlist = pd.DataFrame()
    tokens = []
    if not shortlist.empty and "token_address" in shortlist.columns:
        tokens = [_normalize_evm_address(x) for x in shortlist["token_address"].astype(str).tolist()]
        tokens = [x for x in tokens if x]

    key = str(api_key or "").strip()
    url = _resolve_webhook_url(webhook_url)
    with _STATE_LOCK:
        state = _load_json(state_path)
    stream_id = str(os.getenv("QUICKNODE_ROBINHOOD_STREAM_ID", "") or state.get("stream_id") or "").strip()

    if not key or not url:
        active = bool(stream_id and state.get("security_token"))
        return {
            "status": "RECEIVER_ONLY" if active else "NOT_CONFIGURED",
            "stream_active": active,
            "stream_id": stream_id or None,
            "tokens": len(tokens),
            "webhook_configured": bool(url),
            "api_key_configured": bool(key),
        }

    encoded, filter_hash = _encoded_filter(tokens)
    now = int(time.time())
    created = False
    http_calls = 0

    if not stream_id:
        create_payload = {
            "name": "Raullux Robinhood shortlist transfers",
            "network": STREAM_NETWORK,
            "dataset": STREAM_DATASET,
            "region": "usa_east",
            "dataset_batch_size": max(5, int(batch_size)),
            "elastic_batch_enabled": True,
            "destination": "webhook",
            "destination_attributes": {
                "url": url,
                "compression": "none",
                "max_retry": 5,
                "retry_interval_sec": 2,
                "post_timeout_sec": 10,
            },
            "filter_function": encoded,
            "fix_block_reorgs": 1,
            "keep_distance_from_tip": 2,
            "status": "paused",
        }
        status, body = _stream_api("POST", "", api_key=key, payload=create_payload, timeout=timeout)
        http_calls += 1
        if status not in {200, 201} or not body.get("id"):
            return {"status": "CREATE_ERROR", "http_status": status, "http_calls": http_calls, "tokens": len(tokens), "stream_active": False}
        stream_id = str(body["id"])
        security_token = str((body.get("destination_attributes") or {}).get("security_token") or "")
        with _STATE_LOCK:
            state = _load_json(state_path)
            state.update({
                "stream_id": stream_id,
                "security_token": security_token or state.get("security_token"),
                "filter_hash": filter_hash,
                "watchlist": sorted(set(tokens)),
                "webhook_url": url,
                "stream_status": "paused",
                "stream_last_sync_epoch": now,
                "stream_last_sync_at": _now_iso(),
            })
            _atomic_json(state_path, state)
        created = True

    status, current = _stream_api("GET", f"/{stream_id}", api_key=key, timeout=timeout)
    http_calls += 1
    if status != 200:
        return {"status": "GET_ERROR", "http_status": status, "http_calls": http_calls, "tokens": len(tokens), "stream_id": stream_id, "stream_active": False}

    destination = current.get("destination_attributes") if isinstance(current.get("destination_attributes"), dict) else {}
    security_token = str(destination.get("security_token") or state.get("security_token") or "")
    patch: dict = {}
    if str(state.get("filter_hash") or "") != filter_hash:
        patch["filter_function"] = encoded
    if str(current.get("status") or "").lower() != "active":
        patch["status"] = "active"
    if str(destination.get("url") or "").rstrip("/") != url.rstrip("/"):
        patch["destination_attributes"] = {
            "url": url,
            "compression": "none",
            "max_retry": 5,
            "retry_interval_sec": 2,
            "post_timeout_sec": 10,
        }
    if patch:
        patch_status, patched = _stream_api("PATCH", f"/{stream_id}", api_key=key, payload=patch, timeout=timeout)
        http_calls += 1
        if patch_status != 200:
            return {"status": "PATCH_ERROR", "http_status": patch_status, "http_calls": http_calls, "tokens": len(tokens), "stream_id": stream_id, "stream_active": False}
        current = patched or current

    with _STATE_LOCK:
        state = _load_json(state_path)
        state.update({
            "stream_id": stream_id,
            "security_token": security_token or state.get("security_token"),
            "filter_hash": filter_hash,
            "watchlist": sorted(set(tokens)),
            "webhook_url": url,
            "stream_status": str(current.get("status") or "active"),
            "stream_sequence": current.get("sequence"),
            "stream_last_sync_epoch": now,
            "stream_last_sync_at": _now_iso(),
        })
        _atomic_json(state_path, state)
    return {
        "status": "DONE",
        "stream_active": str(current.get("status") or "").lower() == "active",
        "stream_id": stream_id,
        "tokens": len(tokens),
        "filter_updated": bool(patch.get("filter_function")),
        "created": created,
        "sequence": current.get("sequence"),
        "http_calls": http_calls,
    }


def stream_needs_materialization(state_path: Path) -> bool:
    with _STATE_LOCK:
        state = _load_json(state_path)
    return int(state.get("last_delivery_epoch", 0) or 0) > int(state.get("last_materialized_delivery_epoch", 0) or 0)


def materialize_stream_candidates(
    state_path: Path,
    output_path: Path,
    *,
    rpc_url: str | None,
    timeout: float = 15.0,
    min_cross_token_hits: int = 2,
    max_wallets: int = 100,
    eoa_checks_per_cycle: int = 20,
    max_rps: float = 15.0,
    wallet_ttl_seconds: int = 7 * 86400,
    eoa_ttl_seconds: int = 86400,
) -> dict:
    now = int(time.time())
    now_iso = _now_iso()
    with _STATE_LOCK:
        state = _load_json(state_path)
        wallets = state.get("wallets") if isinstance(state.get("wallets"), dict) else {}
        cutoff = now - max(3600, int(wallet_ttl_seconds))
        wallets = {w: item for w, item in wallets.items() if isinstance(item, dict) and int(item.get("last_seen_epoch", 0) or 0) >= cutoff}

        ranked: list[tuple[str, int, int]] = []
        for wallet, item in wallets.items():
            counts = item.get("token_counts") if isinstance(item.get("token_counts"), dict) else {}
            hits = len([token for token, count in counts.items() if int(count or 0) > 0])
            events = int(item.get("total_events", 0) or 0)
            if hits >= max(1, int(min_cross_token_hits)):
                ranked.append((wallet, hits, events))
        ranked.sort(key=lambda x: (x[1], x[2]), reverse=True)
        ranked = ranked[: max(1, int(max_wallets)) * 3]

        pacing = {"last_request": 0.0}
        min_interval = 1.0 / max(1.0, float(max_rps))
        rpc = str(rpc_url or "").strip()
        checks = rpc_calls = errors = 0
        rows: list[dict] = []
        for wallet, hits, events in ranked:
            if len(rows) >= max(0, int(max_wallets)):
                break
            item = wallets.get(wallet) if isinstance(wallets.get(wallet), dict) else {}
            checked_epoch = int(item.get("eoa_checked_epoch", 0) or 0)
            is_eoa = item.get("is_eoa") if now - checked_epoch < max(0, int(eoa_ttl_seconds)) else None
            if is_eoa is None and rpc and checks < max(0, int(eoa_checks_per_cycle)):
                checks += 1
                try:
                    is_eoa, used = _is_eoa_paced(rpc, wallet, timeout, pacing_state=pacing, min_interval=min_interval)
                    rpc_calls += int(used)
                    item["is_eoa"] = bool(is_eoa)
                    item["eoa_checked_epoch"] = now
                    item["eoa_checked_at"] = now_iso
                    wallets[wallet] = item
                except Exception:
                    errors += 1
                    is_eoa = None
            if is_eoa is not True:
                continue
            rows.append({
                "address": wallet,
                "chain": "robinhood",
                "chain_id": ROBINHOOD_CHAIN_ID,
                "discovery_source": "QUICKNODE_STREAM_TRANSFER_LOGS",
                "independent_cross_token_hits": int(hits),
                "distinct_tokens": int(hits),
                "total_transfer_events": int(events),
                "discovery_score": round(min(100.0, 35.0 + 15.0 * hits + 2.0 * math.log1p(events)), 2),
                "classification": "EOA_NO_CODE",
                "wallet_evidence_state": "DISCOVERY_ONLY",
                "stream_last_seen_at": item.get("last_seen_at"),
            })

        state["wallets"] = wallets
        state["last_materialized_delivery_epoch"] = int(state.get("last_delivery_epoch", 0) or 0)
        state["last_materialized_at"] = now_iso
        _atomic_json(state_path, state)

    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame = frame.sort_values(
            ["independent_cross_token_hits", "total_transfer_events", "discovery_score"],
            ascending=[False, False, False], kind="mergesort",
        ).reset_index(drop=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output_path, index=False)
    return {
        "status": "DONE",
        "wallets": int(len(frame)),
        "tracked": int(len(wallets)),
        "eoa_checks": int(checks),
        "rpc_calls": int(rpc_calls),
        "errors": int(errors),
        "output": str(output_path),
    }


def mark_rpc_backfill(state_path: Path) -> None:
    with _STATE_LOCK:
        state = _load_json(state_path)
        state["last_rpc_backfill_epoch"] = int(time.time())
        state["last_rpc_backfill_at"] = _now_iso()
        _atomic_json(state_path, state)


def run_robinhood_stream_cycle(
    output_dir: Path,
    state_dir: Path,
    *,
    rpc_url: str | None,
    api_key: str | None,
    webhook_url: str | None = None,
    assets_base_url: str = "https://api.robinhood.com/rhj",
    timeout: float = 15.0,
    ttl_seconds: int = 600,
    max_candidates: int = 40,
    max_shortlist: int = 5,
    min_cross_token_hits: int = 2,
    max_wallets: int = 100,
    eoa_checks_per_cycle: int = 20,
    max_rps: float = 15.0,
    rpc_backfill_interval_seconds: int = 21600,
) -> dict:
    base = run_robinhood_radar(
        output_dir,
        state_dir,
        alchemy_api_key=None,
        assets_base_url=assets_base_url,
        timeout=timeout,
        ttl_seconds=ttl_seconds,
        max_candidates=max_candidates,
        max_shortlist=max_shortlist,
        min_cross_token_hits=min_cross_token_hits,
        max_wallets=max_wallets,
    )
    state_path = state_dir / "robinhood_quicknode_stream.json"
    shortlist_path = output_dir / "V22_robinhood_token_radar_shortlist.csv"
    stream = sync_robinhood_stream(
        shortlist_path,
        state_path,
        api_key=api_key,
        webhook_url=webhook_url,
        timeout=timeout,
        batch_size=int(os.getenv("PEIXAO_ROBINHOOD_STREAM_BATCH_SIZE", "20")),
    )
    candidates = materialize_stream_candidates(
        state_path,
        output_dir / "V22_robinhood_stream_wallet_candidates.csv",
        rpc_url=rpc_url,
        timeout=timeout,
        min_cross_token_hits=min_cross_token_hits,
        max_wallets=max_wallets,
        eoa_checks_per_cycle=eoa_checks_per_cycle,
        max_rps=max_rps,
    )
    with _STATE_LOCK:
        state = _load_json(state_path)
    last_backfill = int(state.get("last_rpc_backfill_epoch", 0) or 0)
    backfill_due = int(time.time()) - last_backfill >= max(3600, int(rpc_backfill_interval_seconds))
    active = bool(stream.get("stream_active"))
    return {
        **base,
        "status": "DONE" if active else "PARTIAL",
        "wallets": int(candidates.get("wallets", 0)),
        "wallet_discovery_status": candidates.get("status"),
        "wallet_discovery_provider": "QUICKNODE_STREAM" if active else None,
        "stream": stream,
        "stream_candidates": candidates,
        "stream_active": active,
        "rpc_backfill_due": bool(backfill_due),
        "incremental": True,
    }


def serve_stream_receiver(
    state_path: Path,
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
    security_token: str | None = None,
    signature_max_age_seconds: int = 600,
) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)

    class Handler(BaseHTTPRequestHandler):
        server_version = "RaulluxStream/1.0"

        def _reply(self, status: int, payload: dict):
            raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):  # noqa: N802
            if self.path.rstrip("/") == "/health":
                self._reply(200, {"status": "ok", "stream_receiver": True})
            else:
                self._reply(404, {"status": "not_found"})

        def do_POST(self):  # noqa: N802
            if self.path.split("?", 1)[0].rstrip("/") != STREAM_WEBHOOK_PATH:
                self._reply(404, {"status": "not_found"})
                return
            try:
                length = max(0, int(self.headers.get("Content-Length", "0") or 0))
                raw = self.rfile.read(length)
                if str(self.headers.get("Content-Encoding", "")).lower() == "gzip":
                    raw = gzip.decompress(raw)
                body_text = raw.decode("utf-8")
                with _STATE_LOCK:
                    state = _load_json(state_path)
                token = str(security_token or os.getenv("QUICKNODE_STREAM_SECURITY_TOKEN", "") or state.get("security_token") or "")
                valid, reason = verify_stream_signature(
                    body_text,
                    self.headers,
                    token,
                    max_age_seconds=signature_max_age_seconds,
                )
                if not valid:
                    self._reply(401 if reason != "SECURITY_TOKEN_MISSING" else 503, {"status": reason})
                    return
                payload = json.loads(body_text)
                if not isinstance(payload, dict):
                    self._reply(400, {"status": "INVALID_PAYLOAD"})
                    return
                result = ingest_stream_payload(payload, state_path)
                self._reply(200, result)
            except Exception as exc:
                self._reply(500, {"status": "ERROR", "error": type(exc).__name__})

        def log_message(self, format, *args):  # noqa: A003
            return

    server = ThreadingHTTPServer((host, int(port)), Handler)
    server.daemon_threads = True
    server.serve_forever(poll_interval=0.5)
