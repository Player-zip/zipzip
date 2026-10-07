from __future__ import annotations

from peixao.mobula import probe_mobula


class FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


def test_mobula_not_configured_makes_no_call(tmp_path, monkeypatch):
    called = {"n": 0}

    def fake_get(*args, **kwargs):
        called["n"] += 1
        return FakeResponse(200)

    monkeypatch.setattr("peixao.mobula.requests.get", fake_get)
    result = probe_mobula(tmp_path, api_key=None)
    assert result["status"] == "NOT_CONFIGURED"
    assert result["http_calls"] == 0
    assert result["auth_mode"] == "x_api_key"
    assert called["n"] == 0


def test_mobula_probe_is_cached_and_never_returns_key(tmp_path, monkeypatch):
    calls = []

    def fake_get(url, headers, timeout):
        calls.append((url, headers, timeout))
        return FakeResponse(200)

    monkeypatch.setattr("peixao.mobula.requests.get", fake_get)
    secret = "mbl_test_secret"
    first = probe_mobula(tmp_path, api_key=secret, ttl_seconds=86400)
    second = probe_mobula(tmp_path, api_key=secret, ttl_seconds=86400)

    assert first["status"] == "DONE"
    assert first["http_calls"] == 1
    assert first["cached"] is False
    assert first["probe_endpoint"] == "system_metadata"
    assert first["auth_mode"] == "x_api_key"
    assert second["status"] == "DONE"
    assert second["http_calls"] == 0
    assert second["cached"] is True
    assert len(calls) == 1
    assert calls[0][0].endswith("/2/system-metadata")
    assert calls[0][1]["x-api-key"] == secret
    assert "Authorization" not in calls[0][1]
    state_text = (tmp_path / "mobula_probe.json").read_text(encoding="utf-8")
    assert secret not in str(first)
    assert secret not in str(second)
    assert secret not in state_text
    assert "key_fingerprint" in state_text


def test_old_probe_cache_is_invalidated_once(tmp_path, monkeypatch):
    (tmp_path / "mobula_probe.json").write_text(
        '{"probe_version":4,"probe_endpoint":"system_metadata","auth_mode":"x_api_key","status":"AUTH_ERROR","checked_epoch":9999999999,"http_status":403}',
        encoding="utf-8",
    )
    calls = {"n": 0}

    def fake_get(url, headers, timeout):
        calls["n"] += 1
        return FakeResponse(200)

    monkeypatch.setattr("peixao.mobula.requests.get", fake_get)
    result = probe_mobula(tmp_path, api_key="valid", ttl_seconds=86400)
    assert result["status"] == "DONE"
    assert result["cached"] is False
    assert result["http_calls"] == 1
    assert result["probe_endpoint"] == "system_metadata"
    assert result["auth_mode"] == "x_api_key"
    assert calls["n"] == 1


def test_rotating_key_invalidates_cached_result(tmp_path, monkeypatch):
    responses = [FakeResponse(403), FakeResponse(200)]
    calls = []

    def fake_get(url, headers, timeout):
        calls.append(headers["x-api-key"])
        return responses[len(calls) - 1]

    monkeypatch.setattr("peixao.mobula.requests.get", fake_get)
    first = probe_mobula(tmp_path, api_key="old_key", ttl_seconds=86400)
    second = probe_mobula(tmp_path, api_key="new_key", ttl_seconds=86400)
    third = probe_mobula(tmp_path, api_key="new_key", ttl_seconds=86400)

    assert first["status"] == "AUTH_ERROR"
    assert first["cached"] is False
    assert second["status"] == "DONE"
    assert second["cached"] is False
    assert third["status"] == "DONE"
    assert third["cached"] is True
    assert calls == ["old_key", "new_key"]
    state_text = (tmp_path / "mobula_probe.json").read_text(encoding="utf-8")
    assert "old_key" not in state_text
    assert "new_key" not in state_text


def test_mobula_auth_error_is_safe_and_cached(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_get(*args, **kwargs):
        calls["n"] += 1
        return FakeResponse(401)

    monkeypatch.setattr("peixao.mobula.requests.get", fake_get)
    first = probe_mobula(tmp_path, api_key="bad", ttl_seconds=3600)
    second = probe_mobula(tmp_path, api_key="bad", ttl_seconds=3600)

    assert first["status"] == "AUTH_ERROR"
    assert first["http_status"] == 401
    assert first["auth_mode"] == "x_api_key"
    assert second["cached"] is True
    assert calls["n"] == 1
