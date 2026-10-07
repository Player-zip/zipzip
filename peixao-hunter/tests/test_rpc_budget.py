from datetime import datetime, timezone

from peixao.rpc_budget import RpcBudgetManager
from peixao.pipeline import validation


FIXED_NOW = datetime(2026, 10, 4, 14, 0, 0, tzinfo=timezone.utc)


def test_budget_is_fail_closed_and_tracks_cache_hits(tmp_path):
    manager = RpcBudgetManager(
        tmp_path / "rpc.sqlite3",
        run_limit=3,
        hourly_limit=10,
        daily_limit=20,
        stage_limits={"radar": 0, "tx_origin": 2},
        now_fn=lambda: FIXED_NOW,
    )
    manager.note_cache_hit("tx_origin", 7)

    assert manager.authorize("tx_origin", "primary_evm", "eth_getTransactionByHash") is True
    assert manager.authorize("tx_origin", "primary_evm", "eth_getTransactionByHash") is True
    assert manager.authorize("tx_origin", "primary_evm", "eth_getTransactionByHash") is False
    assert manager.authorize("radar", "primary_evm", "eth_getBalance") is False
    assert manager.authorize("new_unregistered_stage", "primary_evm", "eth_call") is False

    summary = manager.summary()
    assert summary["run_used"] == 2
    assert summary["cache_hits"] == 7
    assert summary["skipped_budget"] == 3
    assert summary["stages"]["radar"]["budget"] == 0
    assert summary["stages"]["tx_origin"]["used"] == 2
    assert summary["fail_closed_unknown_stage"] is True


def test_daily_cap_persists_across_manager_instances(tmp_path):
    db = tmp_path / "rpc.sqlite3"
    first = RpcBudgetManager(
        db,
        run_limit=10,
        hourly_limit=10,
        daily_limit=2,
        stage_limits={"tx_origin": 10},
        now_fn=lambda: FIXED_NOW,
    )
    assert first.authorize("tx_origin", "primary_evm", "method_a") is True
    assert first.authorize("tx_origin", "primary_evm", "method_b") is True

    second = RpcBudgetManager(
        db,
        run_limit=10,
        hourly_limit=10,
        daily_limit=2,
        stage_limits={"tx_origin": 10},
        now_fn=lambda: FIXED_NOW,
    )
    assert second.authorize("tx_origin", "primary_evm", "method_c") is False
    summary = second.summary()
    assert summary["daily_used"] == 2
    assert summary["denied_reasons"]["daily_limit"] == 1


def test_every_retry_costs_budget_and_stops_before_extra_post(tmp_path, monkeypatch):
    manager = RpcBudgetManager(
        tmp_path / "rpc.sqlite3",
        run_limit=10,
        hourly_limit=10,
        daily_limit=10,
        stage_limits={"tx_origin": 2},
        now_fn=lambda: FIXED_NOW,
    )

    class RateLimitedResponse:
        status_code = 429

    class FakeSession:
        def __init__(self):
            self.posts = 0

        def post(self, url, json, timeout):
            self.posts += 1
            return RateLimitedResponse()

    monkeypatch.setattr(validation.time, "sleep", lambda *_: None)
    session = FakeSession()
    result, error, attempts = validation._rpc_call(
        session,
        "https://example.invalid",
        "eth_getTransactionByHash",
        ["0xabc"],
        timeout=1,
        retries=4,
        delay=0,
        call_id=1,
        budget=manager,
        budget_stage="tx_origin",
        provider="primary_evm",
    )

    assert result is None
    assert error == "RPC_BUDGET_EXHAUSTED"
    assert attempts == 2
    assert session.posts == 2
    assert manager.summary()["run_used"] == 2
