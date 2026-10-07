from __future__ import annotations

from hashlib import sha256
import hmac
from pathlib import Path
import time

import pandas as pd

from peixao.execution_queue import build_execution_queue
from peixao import robinhood_stream as rs


def _signed_headers(body: str, secret: str, timestamp: str | None = None):
    nonce = "nonce-1"
    timestamp = timestamp or str(int(time.time()))
    signature = hmac.new(
        secret.encode(),
        (nonce + timestamp + body).encode(),
        sha256,
    ).hexdigest()
    return {
        "X-QN-Nonce": nonce,
        "X-QN-Timestamp": timestamp,
        "X-QN-Signature": signature,
    }


def test_stream_signature_verification():
    body = '{"events":[]}'
    headers = _signed_headers(body, "secret")
    ok, reason = rs.verify_stream_signature(body, headers, "secret")
    assert ok is True
    assert reason == "OK"
    ok, reason = rs.verify_stream_signature(body + "x", headers, "secret")
    assert ok is False
    assert reason == "SIGNATURE_INVALID"


def test_stream_signature_rejects_stale_timestamp():
    body = '{"events":[]}'
    headers = _signed_headers(body, "secret", str(int(time.time()) - 3600))
    ok, reason = rs.verify_stream_signature(body, headers, "secret", max_age_seconds=600)
    assert ok is False
    assert reason == "TIMESTAMP_OUT_OF_RANGE"


def test_stream_ingestion_deduplicates_and_tracks_cross_token(tmp_path: Path):
    state = tmp_path / "state.json"
    token1 = "0x1111111111111111111111111111111111111111"
    token2 = "0x2222222222222222222222222222222222222222"
    wallet = "0x3333333333333333333333333333333333333333"
    rs._atomic_json(state, {"watchlist": [token1, token2]})

    payload = {
        "events": [
            {"token": token1, "from": wallet, "to": "0x4444444444444444444444444444444444444444", "transaction_hash": "0xaaa", "log_index": "0x1", "block_number": "0x10"},
            {"token": token2, "from": wallet, "to": "0x5555555555555555555555555555555555555555", "transaction_hash": "0xbbb", "log_index": "0x2", "block_number": "0x11"},
        ],
        "metadata": {"batch_end_range": 17},
    }
    first = rs.ingest_stream_payload(payload, state)
    second = rs.ingest_stream_payload(payload, state)
    assert first["events_accepted"] == 2
    assert second["events_accepted"] == 0
    assert second["duplicates"] == 2

    saved = rs._load_json(state)
    assert set(saved["wallets"][wallet]["token_counts"]) == {token1, token2}
    assert saved["wallets"][wallet]["total_events"] == 2


def test_stream_candidates_use_small_eoa_budget(tmp_path: Path, monkeypatch):
    state = tmp_path / "state.json"
    output = tmp_path / "stream.csv"
    token1 = "0x1111111111111111111111111111111111111111"
    token2 = "0x2222222222222222222222222222222222222222"
    wallet = "0x3333333333333333333333333333333333333333"
    now = int(time.time())
    rs._atomic_json(state, {
        "last_delivery_epoch": now,
        "wallets": {
            wallet: {
                "token_counts": {token1: 3, token2: 2},
                "total_events": 5,
                "last_seen_epoch": now,
                "last_seen_at": rs._now_iso(),
            }
        },
    })

    calls = []
    def fake_eoa(url, address, timeout, *, pacing_state, min_interval):
        calls.append(address)
        return True, 1

    monkeypatch.setattr(rs, "_is_eoa_paced", fake_eoa)
    result = rs.materialize_stream_candidates(
        state,
        output,
        rpc_url="https://rpc.invalid",
        min_cross_token_hits=2,
        max_wallets=100,
        eoa_checks_per_cycle=1,
    )
    frame = pd.read_csv(output)
    assert result["wallets"] == 1
    assert result["rpc_calls"] == 1
    assert calls == [wallet]
    assert frame.iloc[0]["discovery_source"] == "QUICKNODE_STREAM_TRANSFER_LOGS"
    assert int(frame.iloc[0]["independent_cross_token_hits"]) == 2


def test_execution_queue_merges_rpc_and_stream_candidate(tmp_path: Path):
    output = tmp_path / "output"
    state = tmp_path / "state"
    output.mkdir()
    wallet = "0x3333333333333333333333333333333333333333"
    pd.DataFrame([{
        "address": wallet,
        "chain": "robinhood",
        "independent_cross_token_hits": 2,
        "total_transfer_events": 4,
        "discovery_score": 70,
        "discovery_source": "DRPC_INCREMENTAL_TRANSFER_LOGS",
    }]).to_csv(output / "V22_robinhood_wallet_candidates.csv", index=False)
    pd.DataFrame([{
        "address": wallet,
        "chain": "robinhood",
        "independent_cross_token_hits": 3,
        "total_transfer_events": 8,
        "discovery_score": 82,
        "discovery_source": "QUICKNODE_STREAM_TRANSFER_LOGS",
    }]).to_csv(output / "V22_robinhood_stream_wallet_candidates.csv", index=False)

    result = build_execution_queue(output, state)
    queue = pd.read_csv(output / "V22_execution_queue.csv")
    assert result["wallets"] == 1
    assert int(queue.iloc[0]["independent_cross_token_hits"]) == 3
    assert int(queue.iloc[0]["total_transfer_events"]) == 8
    assert "DRPC_INCREMENTAL_TRANSFER_LOGS" in queue.iloc[0]["discovery_source"]
    assert "QUICKNODE_STREAM_TRANSFER_LOGS" in queue.iloc[0]["discovery_source"]
    assert int(queue.iloc[0]["execution_seen_count"]) == 1


def test_filter_contains_watchlist_and_transfer_topic():
    token = "0x1111111111111111111111111111111111111111"
    code = rs._filter_code([token])
    assert token in code
    assert rs.TRANSFER_TOPIC in code
    assert "return null" in code
