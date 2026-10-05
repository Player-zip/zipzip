from __future__ import annotations

import json

from . import robinhood_stream as _legacy


# QuickNode REST uses the block_with_receipts enum for Robinhood Chain Streams.
# Keep the public receiver/materializer API from robinhood_stream.py, but make
# stream creation and filtering match that dataset.
STREAM_DATASET = "block_with_receipts"
STREAM_NETWORK = _legacy.STREAM_NETWORK
TRANSFER_TOPIC = _legacy.TRANSFER_TOPIC
_LAST_API_ERROR: str | None = None
_ORIGINAL_STREAM_API = _legacy._stream_api
_ORIGINAL_SYNC = _legacy.sync_robinhood_stream


def _filter_code(tokens: list[str]) -> str:
    normalized = sorted(
        set(
            _legacy._normalize_evm_address(value)
            for value in tokens
            if _legacy._normalize_evm_address(value)
        )
    )
    watch = json.dumps(normalized)
    return f'''function main(stream) {{
  const WATCH = new Set({watch});
  const TRANSFER = "{TRANSFER_TOPIC}";
  const rows = Array.isArray(stream.data) ? stream.data : (stream.data ? [stream.data] : []);
  const events = [];

  for (const row of rows) {{
    if (!row || typeof row !== "object") continue;
    const block = row.block && typeof row.block === "object" ? row.block : row;
    const receipts = Array.isArray(row.receipts)
      ? row.receipts
      : (Array.isArray(block.receipts) ? block.receipts : []);
    const blockNumber = block.number || row.blockNumber || row.block_number || null;

    for (const receipt of receipts) {{
      if (!receipt || typeof receipt !== "object") continue;
      const logs = Array.isArray(receipt.logs) ? receipt.logs : [];
      const receiptTxHash = receipt.transactionHash || receipt.transaction_hash || null;

      for (const log of logs) {{
        if (!log || !Array.isArray(log.topics) || log.topics.length < 3) continue;
        const topic0 = String(log.topics[0] || "").toLowerCase();
        const token = String(log.address || "").toLowerCase();
        if (topic0 !== TRANSFER || !WATCH.has(token)) continue;

        const from = "0x" + String(log.topics[1] || "").replace(/^0x/, "").slice(-40).toLowerCase();
        const to = "0x" + String(log.topics[2] || "").replace(/^0x/, "").slice(-40).toLowerCase();
        events.push({{
          token,
          from,
          to,
          block_number: log.blockNumber || blockNumber,
          transaction_hash: log.transactionHash || receiptTxHash,
          log_index: log.logIndex,
          removed: Boolean(log.removed)
        }});
      }}
    }}
  }}

  if (!events.length) return null;
  return {{events, metadata: stream.metadata}};
}}'''


def _safe_api_error(body: dict) -> str | None:
    for key in ("error", "message", "detail"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:600]
        if isinstance(value, (list, dict)):
            try:
                return json.dumps(value, ensure_ascii=False)[:600]
            except Exception:
                pass
    value = body.get("errors")
    if isinstance(value, (list, dict)):
        try:
            return json.dumps(value, ensure_ascii=False)[:600]
        except Exception:
            pass
    return None


def _stream_api_with_diagnostics(method: str, path: str, *, api_key: str, payload=None, timeout: float = 15.0):
    global _LAST_API_ERROR
    effective_payload = payload
    # The free Streams plan does not expose real-time reorg handling, custom
    # distance from tip, or batches above one block. Our 6h RPC reconciliation
    # remains the safety net for rare reorg drift.
    if method.upper() == "POST" and not path and isinstance(payload, dict):
        effective_payload = {
            **payload,
            "dataset_batch_size": 1,
            "fix_block_reorgs": 0,
            "keep_distance_from_tip": 0,
        }
    status, body = _ORIGINAL_STREAM_API(
        method,
        path,
        api_key=api_key,
        payload=effective_payload,
        timeout=timeout,
    )
    _LAST_API_ERROR = _safe_api_error(body) if status >= 400 else None
    return status, body


def _sync_robinhood_stream_free_safe(
    shortlist_path,
    state_path,
    *,
    api_key,
    webhook_url=None,
    timeout: float = 15.0,
    batch_size: int = 20,
):
    result = _ORIGINAL_SYNC(
        shortlist_path,
        state_path,
        api_key=api_key,
        webhook_url=webhook_url,
        timeout=timeout,
        batch_size=1,
    )
    if result.get("status") in {"CREATE_ERROR", "GET_ERROR", "PATCH_ERROR"} and _LAST_API_ERROR:
        result = {**result, "api_error": _LAST_API_ERROR}
    return result


# Patch the original module's runtime globals. worker_daemon imports the receiver
# functions before stream_discovery, so changing these globals preserves one
# shared state/receiver implementation without duplicating webhook code.
_legacy.STREAM_DATASET = STREAM_DATASET
_legacy._filter_code = _filter_code
_legacy._stream_api = _stream_api_with_diagnostics
_legacy.sync_robinhood_stream = _sync_robinhood_stream_free_safe

# Re-export the stable API used by the worker and discovery scheduler.
sync_robinhood_stream = _legacy.sync_robinhood_stream
run_robinhood_stream_cycle = _legacy.run_robinhood_stream_cycle
materialize_stream_candidates = _legacy.materialize_stream_candidates
stream_needs_materialization = _legacy.stream_needs_materialization
serve_stream_receiver = _legacy.serve_stream_receiver
ingest_stream_payload = _legacy.ingest_stream_payload
verify_stream_signature = _legacy.verify_stream_signature
extract_stream_events = _legacy.extract_stream_events
