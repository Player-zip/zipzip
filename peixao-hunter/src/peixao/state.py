from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import os


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class PipelineState:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.state_file = state_dir / "pipeline_state.json"
        self.run_log = state_dir / "pipeline_runs.jsonl"
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict:
        if not self.state_file.exists():
            return {"version": 2, "stages": {}, "runs": 0}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"version": 2, "stages": {}, "runs": 0}
        except Exception:
            return {"version": 2, "stages": {}, "runs": 0}

    def save(self, state: dict) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def log(self, event: dict) -> None:
        with self.run_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
