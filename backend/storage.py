from __future__ import annotations

import re
import threading
from collections import Counter
from pathlib import Path

from backend.config import DOWNLOAD_DIR

_APP_FILE = re.compile(r"^[a-f0-9]{32}\.(?:mp4|mp3)$")
_JOB_DIR = re.compile(r"^[a-f0-9]{32}$")
_lock = threading.RLock()
_active: Counter[str] = Counter()


def register_active(job_id: str) -> None:
    with _lock:
        _active[job_id] += 1


def release_active(job_id: str) -> None:
    with _lock:
        count = _active.get(job_id, 0)
        if count <= 1:
            _active.pop(job_id, None)
        else:
            _active[job_id] = count - 1


def acquire_download_file(filename: str) -> Path | None:
    """Atomically resolve and pin an app output against TTL cleanup."""
    if not _APP_FILE.fullmatch(filename):
        return None
    job_id = filename.rsplit(".", 1)[0]
    with _lock:
        try:
            root = DOWNLOAD_DIR.resolve()
            candidate = root / filename
            if candidate.is_symlink():
                return None
            path = candidate.resolve(strict=True)
            if path.parent != root or not path.is_file():
                return None
        except OSError:
            return None
        _active[job_id] += 1
        return path


def cleanup_expired(download_dir: Path, temp_dir: Path, cutoff: float, logger) -> None:
    """Delete only expired TikDrop outputs and UUID job directories in-root."""
    roots = ((download_dir, False), (temp_dir, True))
    with _lock:
        for configured_root, is_temp in roots:
            try:
                root = configured_root.resolve(strict=True)
                for item in root.iterdir():
                    try:
                        if item.is_symlink():
                            continue
                        resolved = item.resolve(strict=True)
                        if resolved.parent != root or item.stat().st_mtime >= cutoff:
                            continue
                        if is_temp:
                            owned = _JOB_DIR.fullmatch(item.name) and item.is_dir()
                            job_id = item.name
                        else:
                            owned = _APP_FILE.fullmatch(item.name) and item.is_file()
                            job_id = item.name.rsplit(".", 1)[0] if owned else ""
                        if not owned or _active.get(job_id, 0):
                            continue
                        # Keep the active registry lock while removing so a new
                        # job/response cannot acquire the same ID mid-cleanup.
                        if is_temp:
                            import shutil
                            shutil.rmtree(item)
                        else:
                            item.unlink()
                    except OSError as exc:
                        logger.warning("Cleanup gagal untuk item TikDrop %s (%s)", item.name, type(exc).__name__)
            except OSError as exc:
                logger.warning("Cleanup tidak dapat membaca direktori TikDrop (%s)", type(exc).__name__)
