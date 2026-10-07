from __future__ import annotations

import pandas as pd

from peixao.birdeye_alpha import run_birdeye_alpha_discovery


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self):
        self.get_calls = []
        self.post_calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.get_calls.append((url, params))
        if "top_traders" in url:
            token = params["address"]
            return FakeResponse(200, {"data": {"items": [
                {"owner": "WalletAlpha111111111111111111111111111111111", "realizedPnl": 5000, "tags": []},
                {"owner": "WalletDev11111111111111111111111111111111111", "realizedPnl": 9000, "tags": ["dev"]},
                {"owner": f"Only{token[:8]}11111111111111111111111111111111111", "realizedPnl": 2000, "tags": []},
            ]}})
        return FakeResponse(200, {"data": {"summary": {"win_rate": 0.70, "unique_tokens": 12, "total_trades": 25, "realized_profit": 18000}}})

    def post(self, url, headers=None, json=None, timeout=None):
        self.post_calls.append((url, json))
        return FakeResponse(200, {"data": {
            "summary": {"win_rate": 0.70, "unique_tokens": 12, "total_trades": 25, "realized_profit": 18000},
            "items": [
                {"realized_profit": 5000}, {"realized_profit": 3000},
                {"realized_profit": 1000}, {"realized_profit": -500},
            ],
        }})


def test_birdeye_requires_recurrence_rejects_dev_and_enriches_pnl(tmp_path, monkeypatch):
    shortlist = tmp_path / "shortlist.csv"
    pd.DataFrame([
        {"token_address": "TokenA111111111111111111111111111111111111", "radar_score": 90},
        {"token_address": "TokenB111111111111111111111111111111111111", "radar_score": 85},
    ]).to_csv(shortlist, index=False)
    session = FakeSession()
    monkeypatch.setattr("peixao.birdeye_alpha.requests.Session", lambda: session)

    result = run_birdeye_alpha_discovery(
        shortlist,
        tmp_path / "output",
        tmp_path / "state",
        api_key="secret",
        top_traders_per_token=10,
        max_tokens=2,
        min_cross_token_hits=2,
        max_pnl_wallets=5,
        delay=0,
    )

    assert result["status"] == "DONE"
    assert result["recurrent_wallets"] == 1
    assert result["enriched_wallets"] == 1
    enriched = pd.read_csv(tmp_path / "output" / "V22_radar_wallet_enriched.csv")
    row = enriched.iloc[0]
    assert row["address"] == "WalletAlpha111111111111111111111111111111111"
    assert row["cross_token_hits"] == 2
    assert abs(row["win_rate"] - 0.70) < 1e-9
    assert row["closed_positions"] == 4
    assert row["winning_tokens"] == 3
    assert row["new_positions_per_week"] < 3
    assert "Dev" not in "".join(enriched["address"].astype(str).tolist())
    assert len(session.post_calls) == 1
