from __future__ import annotations

from pathlib import Path
import hashlib
import os
import shutil
import zipfile

import gdown

from .config import Settings


DEFAULT_BOOTSTRAP_DRIVE_FILE_ID = "1TpckmalstGf6lh0X_JKpCtfFhS9Lwrex"
DEFAULT_BOOTSTRAP_SHA256 = "9bb082021d6cc69d099deef72d88d972741117280aadd0af627b7069fd17dc4c"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def project_seed_ready(cfg: Settings) -> bool:
    root = cfg.project_root
    cache = cfg.v3_cache_dir
    if not root or not root.is_dir() or not cache or not cache.is_dir():
        return False
    if not (root / "V3B_CHECKPOINT.json").is_file():
        return False
    if len(list(cache.glob("*.json"))) < 27:
        return False
    if not (root / "V5_WALLETS" / "V5_final_candidates.csv").is_file():
        return False
    if not (root / "V6_GMGN" / "V6_wallets_gmgn_summary.csv").is_file():
        return False
    return True


def _safe_extract(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            target = (destination / info.filename).resolve()
            if destination != target and destination not in target.parents:
                raise ValueError(f"unsafe bootstrap archive path: {info.filename}")
        zf.extractall(destination)


def seed_project_if_needed(cfg: Settings) -> dict:
    """One-time Railway bootstrap from a read-only public Drive ZIP.

    The archive contains only cached/on-chain project data and no API secrets.
    After successful extraction it is deleted; subsequent boots use the volume.
    """
    if project_seed_ready(cfg):
        return {"status": "READY", "seeded": False}

    if not cfg.project_root:
        return {"status": "WAITING", "seeded": False, "reason": "PEIXAO_PROJECT_ROOT missing"}

    file_id = os.getenv("PEIXAO_BOOTSTRAP_DRIVE_FILE_ID", DEFAULT_BOOTSTRAP_DRIVE_FILE_ID).strip()
    expected_sha = os.getenv("PEIXAO_BOOTSTRAP_SHA256", DEFAULT_BOOTSTRAP_SHA256).strip().lower()
    if not file_id:
        return {"status": "WAITING", "seeded": False, "reason": "bootstrap Drive file id missing"}

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    archive = cfg.data_dir / ".peixao_bootstrap.zip"
    partial = cfg.data_dir / ".peixao_bootstrap.download"
    partial.unlink(missing_ok=True)

    try:
        downloaded = gdown.download(id=file_id, output=str(partial), quiet=True, fuzzy=False)
        if not downloaded or not partial.is_file():
            partial.unlink(missing_ok=True)
            return {"status": "WAITING", "seeded": False, "reason": "bootstrap not downloadable yet"}

        actual_sha = _sha256(partial)
        if expected_sha and actual_sha != expected_sha:
            partial.unlink(missing_ok=True)
            return {
                "status": "WAITING",
                "seeded": False,
                "reason": "bootstrap sha256 mismatch",
                "actual_sha256": actual_sha,
            }

        os.replace(partial, archive)
        _safe_extract(archive, cfg.data_dir)

        if not project_seed_ready(cfg):
            return {"status": "WAITING", "seeded": False, "reason": "bootstrap extracted but validation failed"}

        return {"status": "SEEDED", "seeded": True, "sha256": actual_sha}
    finally:
        partial.unlink(missing_ok=True)
        archive.unlink(missing_ok=True)
