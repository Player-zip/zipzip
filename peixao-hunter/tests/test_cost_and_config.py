import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from peixao import config
from peixao.bootstrap import _safe_extract
from peixao.quicknode_solana import QuickNodeCreditBudget, SolanaRpcRouter
from peixao.rpc_budget import DailyCallLimiter, rpc_provider_for_url


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, script):
        self.script = script
        self.methods = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.methods.append(json["method"])
        return self.script(json["method"], json["params"])


def _router(tmp_path, script, **kwargs):
    budget = QuickNodeCreditBudget(tmp_path / "qn.json", daily_limit=10_000, run_limit=10_000)
    router = SolanaRpcRouter(quicknode_rpc_url="https://x.quiknode.pro/k", fallback_rpc_urls=[], budget=budget, delay=0, **kwargs)
    router.session = FakeSession(script)
    return router, budget


def test_transient_error_does_not_switch_to_per_account_fanout(tmp_path):
    router, _ = _router(tmp_path, lambda method, params: FakeResponse(429, {}))
    result, _, error = router.call("getMultipleAccounts", [["a", "b", "c"], {"encoding": "jsonParsed"}])
    assert result is None and "HTTP_429" in error
    assert router._multiple_accounts_supported is None
    assert "getAccountInfo" not in router.session.methods


def test_unsupported_method_falls_back_and_is_remembered(tmp_path):
    def script(method, params):
        if method == "getMultipleAccounts":
            return FakeResponse(200, {"error": {"code": -32601, "message": "Method not found"}})
        return FakeResponse(200, {"result": {"value": {"data": {"parsed": {"info": {"owner": params[0]}}}}}})

    router, _ = _router(tmp_path, script)
    result, _, error = router.call("getMultipleAccounts", [["a", "b"], {"encoding": "jsonParsed"}])
    assert error is None and len(result["value"]) == 2
    router.call("getMultipleAccounts", [["c"], {"encoding": "jsonParsed"}])
    assert router.session.methods.count("getMultipleAccounts") == 1


def test_method_credit_estimates(tmp_path):
    router, budget = _router(
        tmp_path,
        lambda method, params: FakeResponse(200, {"result": 1}),
        method_credit_estimates={"getTransaction": 100},
    )
    router.call("getTransaction", ["sig"])
    router.call("getSlot", [])
    assert budget.spent_run == 130


def test_daily_limiter_caps_provider(tmp_path):
    limiter = DailyCallLimiter(tmp_path / "b.sqlite3", now_fn=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert limiter.try_consume("alchemy", limit=2)
    assert limiter.try_consume("alchemy", limit=2)
    assert not limiter.try_consume("alchemy", limit=2)
    assert limiter.try_consume("drpc", limit=0)  # 0 = sem teto
    assert limiter.usage_today()["alchemy"]["denied"] == 1
    assert rpc_provider_for_url("https://base-mainnet.g.alchemy.com/v2/k") == "alchemy"
    assert rpc_provider_for_url("https://rpc.mainnet.chain.robinhood.com") == "robinhood_rpc"


def test_invalid_env_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("PEIXAO_TEST_INT", "abc")
    monkeypatch.setenv("PEIXAO_TEST_FLOAT", "1,5")
    assert config.env_int("PEIXAO_TEST_INT", 7) == 7
    assert config.env_float("PEIXAO_TEST_FLOAT", 2.5) == 2.5
    monkeypatch.setenv("PEIXAO_TEST_INT", "12")
    assert config.env_int("PEIXAO_TEST_INT", 7) == 12


def test_bootstrap_rejects_archives_that_expand_too_much(tmp_path):
    archive = tmp_path / "seed.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("project/big.bin", b"\0" * 200_000)
    with pytest.raises(ValueError, match="too large"):
        _safe_extract(archive, tmp_path / "out", max_uncompressed_bytes=100_000)
    _safe_extract(archive, tmp_path / "ok", max_uncompressed_bytes=1_000_000)
    assert (tmp_path / "ok" / "project" / "big.bin").is_file()


def test_runner_provider_probes_are_optional_stages():
    source = (Path(__file__).resolve().parents[1] / "src" / "peixao" / "runner.py").read_text(encoding="utf-8")
    for stage in ("08_gmgn_live_probe", "08b_mobula_probe", "08c_jupiter_probe", "08d_solana_tracker_probe", "08e_dune_probe"):
        assert f'optional_stage("{stage}"' in source
    assert "notify_alpha_wallets" not in source
