from types import SimpleNamespace

import pandas as pd

from peixao import gmgn_live


def test_demo_probe_runs_once(tmp_path, monkeypatch):
    output_dir = tmp_path / "output"
    state_dir = tmp_path / "state"
    output_dir.mkdir()
    pd.DataFrame([
        {"address": "0xABC", "peixao_score_v1": 81.0},
    ]).to_csv(output_dir / "V6_peixao_ranked_v1.csv", index=False)

    monkeypatch.setattr(gmgn_live, "_find_cli", lambda: "/fake/gmgn-cli")
    calls = []

    def fake_run(cmd, env, text, capture_output, timeout, check):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout='{"data": {"ok": true}}', stderr="")

    monkeypatch.setattr(gmgn_live.subprocess, "run", fake_run)

    first = gmgn_live.probe_gmgn_live(
        output_dir,
        state_dir,
        api_key=None,
        demo_enabled=True,
        chain="robinhood",
        timeout=5,
    )
    assert first["status"] == "DONE"
    assert first["mode"] == "DEMO_TEST"
    assert first["commands_ok"] == 4
    assert first["command_statuses"] == {
        "stats": "OK",
        "profits_30d": "OK",
        "holdings": "OK",
        "activity": "OK",
    }
    assert len(calls) == 4
    assert (output_dir / "V6_gmgn_live_probe.json").is_file()
    assert (state_dir / "gmgn_demo_probe_done.json").is_file()

    second = gmgn_live.probe_gmgn_live(
        output_dir,
        state_dir,
        api_key=None,
        demo_enabled=True,
        chain="robinhood",
        timeout=5,
    )
    assert second["status"] == "SKIPPED_DEMO_ALREADY_PROBED"
    assert second["previous_status"] == "DONE"
    assert second["commands_ok"] == 4
    assert second["commands_total"] == 4
    assert second["command_statuses"]["stats"] == "OK"
    assert len(calls) == 4


def test_persisted_partial_probe_surfaces_safe_statuses(tmp_path):
    output_dir = tmp_path / "output"
    state_dir = tmp_path / "state"
    output_dir.mkdir()
    state_dir.mkdir()
    (state_dir / "gmgn_demo_probe_done.json").write_text("{}", encoding="utf-8")
    (output_dir / "V6_gmgn_live_probe.json").write_text(
        '{"status":"PARTIAL","mode":"DEMO_TEST","chain":"robinhood","wallet":"0xabc","checked_at":"2026-10-04T13:00:00Z","commands_ok":1,"commands_total":4,"results":{"stats":{"status":"OK","data":{"secret":"not surfaced"}},"profits_30d":{"status":"AUTH_ERROR","stderr":"hidden diagnostic"},"holdings":{"status":"ERROR"},"activity":{"status":"TIMEOUT"}}}',
        encoding="utf-8",
    )

    result = gmgn_live.probe_gmgn_live(
        output_dir,
        state_dir,
        api_key=None,
        demo_enabled=True,
        chain="robinhood",
        timeout=5,
    )
    assert result["status"] == "SKIPPED_DEMO_ALREADY_PROBED"
    assert result["previous_status"] == "PARTIAL"
    assert result["commands_ok"] == 1
    assert result["command_statuses"] == {
        "stats": "OK",
        "profits_30d": "AUTH_ERROR",
        "holdings": "ERROR",
        "activity": "TIMEOUT",
    }
    assert "stderr" not in str(result)
    assert "secret" not in str(result)


def test_probe_without_wallet_is_safe(tmp_path, monkeypatch):
    output_dir = tmp_path / "output"
    state_dir = tmp_path / "state"
    output_dir.mkdir()
    monkeypatch.setattr(gmgn_live, "_find_cli", lambda: "/fake/gmgn-cli")

    result = gmgn_live.probe_gmgn_live(
        output_dir,
        state_dir,
        api_key=None,
        demo_enabled=True,
        chain="robinhood",
        timeout=5,
    )
    assert result["status"] == "SKIPPED_NO_WALLET"
