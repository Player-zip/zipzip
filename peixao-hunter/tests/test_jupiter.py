from __future__ import annotations

from peixao.jupiter import probe_jupiter


class FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = [] if payload is None else payload

    def json(self):
        return self._payload


def test_jupiter_not_configured_makes_no_call(tmp_path, monkeypatch):
    called = {"n": 0}

    def fake_get(*args, **kwargs):
        called["n"] += 1
        return FakeResponse(200)

    monkeypatch.setattr("peixao.jupiter.requests.get", fake_get)
    result = probe_jupiter(tmp_path, api_key=None)
    assert result["status"] == "NOT_CONFIGURED"
    assert result["http_calls"] == 0
    assert result["auth_mode"] == "x_api_key"
    assert called["n"] == 0


def test_jupiter_probe_uses_top_organic_and_is_cached(tmp_path, monkeypatch):
    calls = []

    def fake_get(url, headers, timeout):
        calls.append((url, headers, timeout))
        return FakeResponse(200, [{"id": "a"}, {"id": "b"}])

    monkeypatch.setattr("peixao.jupiter.requests.get", fake_get)
    secret = "jup_test_secret"
    first = probe_jupiter(tmp_path, api_key=secret, ttl_seconds=86400)
    second = probe_jupiter(tmp_path, api_key=secret, ttl_seconds=86400)

    assert first["status"] == "DONE"
    assert first["http_calls"] == 1
    assert first["cached"] is False
    assert first["items_seen"] == 2
    assert first["probe_endpoint"] == "tokens_top_organic_5m"
    assert second["status"] == "DONE"
    assert second["http_calls"] == 0
    assert second["cached"] is True
    assert len(calls) == 1
    assert calls[0][0].endswith("/tokens/v2/toporganicscore/5m")
    assert calls[0][1]["x-api-key"] == secret
    assert secret not in str(first)
    assert secret not in str(second)
    assert secret not in (tmp_path / "jupiter_probe.json").read_text(encoding="utf-8")


def test_jupiter_key_rotation_invalidates_cached_auth_error(tmp_path, monkeypatch):
    calls = []

    def fake_get(url, headers, timeout):
        calls.append(headers["x-api-key"])
        if headers["x-api-key"] == "old-key":
            return FakeResponse(403)
        return FakeResponse(200, [{"id": "ok"}])

    monkeypatch.setattr("peixao.jupiter.requests.get", fake_get)
    first = probe_jupiter(tmp_path, api_key="old-key", ttl_seconds=86400)
    second = probe_jupiter(tmp_path, api_key="new-key", ttl_seconds=86400)

    assert first["status"] == "AUTH_ERROR"
    assert second["status"] == "DONE"
    assert second["cached"] is False
    assert second["http_calls"] == 1
    assert calls == ["old-key", "new-key"]


def test_jupiter_rate_limit_is_classified(tmp_path, monkeypatch):
    def fake_get(*args, **kwargs):
        return FakeResponse(429)

    monkeypatch.setattr("peixao.jupiter.requests.get", fake_get)
    result = probe_jupiter(tmp_path, api_key="valid", ttl_seconds=0)
    assert result["status"] == "RATE_LIMITED"
    assert result["http_status"] == 429
