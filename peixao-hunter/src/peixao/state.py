from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import logging
import os
import traceback

logger = logging.getLogger("peixao")


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


def safe_call(name: str, fn):
    """Executa uma etapa isolada: erro vira status ERROR, com traceback no log."""
    try:
        return fn()
    except Exception as exc:
        logger.exception("etapa %s falhou", name)
        return {
            "status": "ERROR",
            "stage": name,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(limit=6)[-2000:],
        }


def persist_summary(state_dir: Path, updates: dict, event: dict) -> None:
    """Grava o resumo do ciclo no estado e no log de execuções sem derrubar o ciclo."""
    try:
        store = PipelineState(state_dir)
        state = store.load()
        state.update(updates)
        store.save(state)
        store.log(event)
    except Exception:
        logger.exception("falha ao persistir o estado do pipeline")


def atomic_csv(frame, path: Path) -> None:
    """Escreve CSV via arquivo temporário + rename (leitor nunca vê arquivo pela metade)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    frame.to_csv(tmp, index=False)
    os.replace(tmp, path)
