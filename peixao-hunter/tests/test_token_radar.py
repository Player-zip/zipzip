from __future__ import annotations

import pandas as pd

from peixao.token_radar import run_token_radar


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not (200 <= self.status_code < 300):
            raise RuntimeError(f"HTTP {self.status_code}")


def test_radar_uses_jupiter_plus_one_dex_batch_and_zero_rpc(tmp_path, monkeypatch):
    token = "7YttLkHDoJfw7B3NsYWjJzUhNPc6Q8ALq6bB6pYqpump"
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        if "toporganicscore" in url:
            return FakeResponse(200, [{
                "id": token,
                "symbol": "PEIXE",
                "organicScore": 0.95,
                "holderCount": 1500,
            }])
        return FakeResponse(200, [{
            "chainId": "solana",
            "dexId": "raydium",
            "pairAddress": "pair1",
            "baseToken": {"address": token},
            "priceUsd": "0.01",
            "marketCap": 1000000,
            "liquidity": {"usd": 250000},
            "volume": {"h24": 500000},
            "txns": {"h1": {"buys": 80, "sells": 20}},
            "pairCreatedAt": 1,
        }])

    monkeypatch.setattr("peixao.token_radar.requests.get", fake_get)
    output = tmp_path / "output"
    state = tmp_path / "state"
    db = tmp_path / "master.sqlite3"
    result = run_token_radar(
        output, state, db,
        jupiter_api_key="secret",
        ttl_seconds=1800,
        min_radar_score=40,
    )

    assert result["status"] == "DONE"
    assert result["rpc_calls"] == 0
    assert result["http_calls"] == 2
    assert result["tokens_seen"] == 1
    assert result["shortlisted"] == 1
    assert result["snapshots_inserted"] == 1
    assert len(calls) == 2
    shortlist = pd.read_csv(output / "V22_token_radar_shortlist.csv")
    assert shortlist.iloc[0]["token_address"] == token


def test_radar_cache_avoids_repeat_http_calls(tmp_path, monkeypatch):
    token = "7YttLkHDoJfw7B3NsYWjJzUhNPc6Q8ALq6bB6pYqcache"
    calls = {"n": 0}

    def fake_get(url, headers=None, timeout=None):
        calls["n"] += 1
        if "toporganicscore" in url:
            return FakeResponse(200, [{"id": token, "symbol": "CACHE", "organicScore": 0.9}])
        return FakeResponse(200, [{
            "chainId": "solana", "baseToken": {"address": token},
            "liquidity": {"usd": 100000}, "volume": {"h24": 100000},
            "txns": {"h1": {"buys": 10, "sells": 5}},
        }])

    monkeypatch.setattr("peixao.token_radar.requests.get", fake_get)
    args = (tmp_path / "output", tmp_path / "state", tmp_path / "master.sqlite3")
    first = run_token_radar(*args, jupiter_api_key="secret", min_radar_score=0)
    second = run_token_radar(*args, jupiter_api_key="secret", min_radar_score=0)
    assert first["http_calls"] == 2
    assert second["http_calls"] == 0
    assert second["cached"] is True
    assert calls["n"] == 2
