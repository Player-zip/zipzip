from __future__ import annotations

from peixao.dune import probe_dune


class FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = {} if payload is None else payload

    def json(self):
        return self._payload


def test_dune_not_configured_makes_no_call(tmp_path, monkeypatch):
    called = {"n": 0}

    def fake_post(*args, **kwargs):
        called["n"] += 1
        return FakeResponse(200)

    monkeypatch.setattr("peixao.dune.requests.post", fake_post)
    result = probe_dune(tmp_path, api_key=None)
    assert result["status"] == "NOT_CONFIGURED"
    assert result["http_calls"] == 0
    assert result["auth_mode"] == "x_dune_api_key"
    assert called["n"] == 0


def test_dune_usage_probe_is_cached_and_exposes_safe_usage(tmp_path, monkeypatch):
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append((url, headers, json, timeout))
        return FakeResponse(
            200,
            {
                "billing_periods": [
                    {"credits_used": 12.5, "credits_included": 1000}
                ]
            },
        )

    monkeypatch.setattr("peixao.dune.requests.post", fake_post)
    secret = "dune_test_secret"
    first = probe_dune(tmp_path, api_key=secret, ttl_seconds=86400)
    second = probe_dune(tmp_path, api_key=secret, ttl_seconds=86400)

    assert first["status"] == "DONE"
    assert first["http_calls"] == 1
    assert first["cached"] is False
    assert first["credits_used"] == 12.5
    assert first["credits_included"] == 1000
    assert first["billing_periods_seen"] == 1
    assert second["status"] == "DONE"
    assert second["http_calls"] == 0
    assert second["cached"] is True
    assert len(calls) == 1
    assert calls[0][0].endswith("/api/v1/usage")
    assert calls[0][1]["X-DUNE-API-KEY"] == secret
    assert calls[0][2] == {}
    assert secret not in str(first)
    assert secret not in str(second)
    assert secret not in (tmp_path / "dune_probe.json").read_text(encoding="utf-8")


def test_dune_key_rotation_invalidates_cache(tmp_path, monkeypatch):
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append(headers["X-DUNE-API-KEY"])
        if headers["X-DUNE-API-KEY"] == "old-key":
            return FakeResponse(403)
        return FakeResponse(200, {"billing_periods": []})

    monkeypatch.setattr("peixao.dune.requests.post", fake_post)
    first = probe_dune(tmp_path, api_key="old-key", ttl_seconds=86400)
    second = probe_dune(tmp_path, api_key="new-key", ttl_seconds=86400)

    assert first["status"] == "AUTH_ERROR"
    assert second["status"] == "DONE"
    assert second["cached"] is False
    assert second["http_calls"] == 1
    assert calls == ["old-key", "new-key"]


def test_dune_rate_limit_is_classified(tmp_path, monkeypatch):
    def fake_post(*args, **kwargs):
        return FakeResponse(429)

    monkeypatch.setattr("peixao.dune.requests.post", fake_post)
    result = probe_dune(tmp_path, api_key="valid", ttl_seconds=0)
    assert result["status"] == "RATE_LIMITED"
    assert result["http_status"] == 429
