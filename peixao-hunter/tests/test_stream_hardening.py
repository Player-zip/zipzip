import gzip
import http.client
import json
import threading
import time
from hashlib import sha256
import hmac
from pathlib import Path

from peixao import robinhood_chainstack as rc
from peixao import robinhood_stream as rs

T1 = "0x" + "1" * 40
T2 = "0x" + "2" * 40
W = "0x" + "a" * 40


def _event(token, i):
    return {"token": token, "from": W, "to": "0x" + f"{i:040x}", "transaction_hash": f"0x{i}", "log_index": i, "block_number": i}


def _seed(state: Path) -> None:
    rs.ingest_stream_payload({"events": [_event(T1, 1), _event(T2, 2)]}, state)


def test_rpc_failure_is_unknown_not_contract(tmp_path, monkeypatch):
    state = tmp_path / "s.json"
    _seed(state)
    monkeypatch.setattr(rs, "_is_eoa_paced", lambda *a, **k: (None, 5))
    result = rs.materialize_stream_candidates(state, tmp_path / "c.csv", rpc_url="http://rpc", min_cross_token_hits=2)
    assert result["wallets"] == 0
    assert result["errors"] == 1
    assert "is_eoa" not in rs._load_json(state)["wallets"][W]

    monkeypatch.setattr(rs, "_is_eoa_paced", lambda *a, **k: (True, 1))
    retry = rs.materialize_stream_candidates(state, tmp_path / "c.csv", rpc_url="http://rpc", min_cross_token_hits=2)
    assert retry["wallets"] == 1


def test_paced_rpc_429_returns_unknown(monkeypatch):
    class Response:
        status_code = 429

        def json(self):
            return {}

        def raise_for_status(self):
            return None

    monkeypatch.setattr(rc.requests, "post", lambda *a, **k: Response())
    monkeypatch.setattr(rc.time, "sleep", lambda s: None)
    is_eoa, attempts = rc._is_eoa_paced("http://rpc", W, 1.0, pacing_state={}, min_interval=0.0)
    assert is_eoa is None
    assert attempts >= 1


def test_missing_rpc_url_is_reported(tmp_path):
    state = tmp_path / "s.json"
    _seed(state)
    result = rs.materialize_stream_candidates(state, tmp_path / "c.csv", rpc_url=None, min_cross_token_hits=2)
    assert result["status"] == "RPC_URL_MISSING"
    assert result["eoa_pending"] == 1


def test_webhook_is_not_blocked_by_eoa_checks(tmp_path, monkeypatch):
    state = tmp_path / "s.json"
    _seed(state)

    def slow_eoa(*args, **kwargs):
        time.sleep(0.6)
        return True, 1

    monkeypatch.setattr(rs, "_is_eoa_paced", slow_eoa)
    worker = threading.Thread(
        target=rs.materialize_stream_candidates,
        args=(state, tmp_path / "c.csv"),
        kwargs={"rpc_url": "http://rpc", "min_cross_token_hits": 2},
    )
    worker.start()
    time.sleep(0.15)
    started = time.monotonic()
    rs.ingest_stream_payload({"events": [_event(T1, 3)]}, state)
    waited = time.monotonic() - started
    worker.join()
    assert waited < 0.3
    saved = rs._load_json(state)
    assert saved["wallets"][W]["is_eoa"] is True
    assert saved["wallets"][W]["total_events"] == 3  # a entrega do meio não foi perdida
    # A entrega que chegou durante as checagens dispara outra materialização.
    assert rs.stream_needs_materialization(state) is True


def test_v2_module_no_longer_patches_the_stream_module():
    before = rs.sync_robinhood_stream
    import peixao.robinhood_stream_v2 as rs2

    assert rs.sync_robinhood_stream is before
    assert rs2.sync_robinhood_stream is before
    assert rs.STREAM_DATASET == "block_with_receipts"


def test_bounded_gunzip_rejects_bombs():
    bomb = gzip.compress(b"\0" * (2 * 1024 * 1024))
    try:
        rs._bounded_gunzip(bomb, 1024 * 1024)
    except rs.PayloadTooLarge:
        pass
    else:
        raise AssertionError("gzip bomb accepted")
    assert rs._bounded_gunzip(gzip.compress(b"ok"), 1024) == b"ok"


def _signed(body: str, secret: str, nonce: str):
    timestamp = str(int(time.time()))
    signature = hmac.new(secret.encode(), (nonce + timestamp + body).encode(), sha256).hexdigest()
    return {"X-QN-Nonce": nonce, "X-QN-Timestamp": timestamp, "X-QN-Signature": signature, "Content-Type": "application/json"}


def test_webhook_limits_and_replay(tmp_path):
    state = tmp_path / "s.json"
    server = rs.build_stream_server(state, host="127.0.0.1", port=0, security_token="secret", max_body_bytes=4096, max_decompressed_bytes=8192)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        def post(body: bytes, headers: dict):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", rs.STREAM_WEBHOOK_PATH, body=body, headers=headers)
            response = conn.getresponse()
            payload = json.loads(response.read() or b"{}")
            conn.close()
            return response.status, payload

        status, _ = post(b"x" * 5000, {"Content-Type": "application/json"})
        assert status == 413

        bomb = gzip.compress(b" " * 100_000)
        status, _ = post(bomb, {"Content-Encoding": "gzip", **_signed("", "secret", "n0")})
        assert status == 413

        body = json.dumps({"events": [_event(T1, 7)]})
        headers = _signed(body, "secret", "nonce-1")
        status, payload = post(body.encode(), headers)
        assert status == 200 and payload["events_accepted"] == 1
        status, payload = post(body.encode(), headers)
        assert status == 200 and payload["status"] == "DUPLICATE_DELIVERY"

        status, payload = post(body.encode(), {**headers, "X-QN-Signature": "0" * 64, "X-QN-Nonce": "nonce-2"})
        assert status == 401
    finally:
        server.shutdown()
        server.server_close()
