from __future__ import annotations

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=False)

ACCESS_PASSWORD = os.getenv("TIKDROP_ACCESS_PASSWORD", "")
if ACCESS_PASSWORD and (not ACCESS_PASSWORD.strip() or len(ACCESS_PASSWORD) < 16):
    raise ValueError("TIKDROP_ACCESS_PASSWORD harus berisi minimal 16 karakter non-kosong.")


def _ensure_winget_ffmpeg_on_path() -> None:
    """Find Gyan FFmpeg installed by WinGet even when the shell PATH is stale."""
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return

    local_app_data = os.getenv("LOCALAPPDATA")
    if not local_app_data:
        return

    packages_dir = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
    if not packages_dir.is_dir():
        return

    try:
        for package_dir in packages_dir.glob("Gyan.FFmpeg*"):
            for executable in package_dir.glob("**/bin/ffmpeg.exe"):
                bin_dir = executable.parent
                if (bin_dir / "ffprobe.exe").is_file():
                    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
                    return
    except OSError:
        # The application can still run; /api/health will report tool availability.
        return


_ensure_winget_ffmpeg_on_path()
FFMPEG_PATH = shutil.which("ffmpeg")
FFPROBE_PATH = shutil.which("ffprobe")


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _duration_limit_seconds() -> int:
    name = "TIKDROP_MAX_DURATION_SECONDS"
    raw = os.getenv(name)
    if raw is None:
        return 600
    if not raw.strip():
        raise ValueError(f"{name} harus berupa bilangan bulat positif.")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} harus berupa bilangan bulat positif.") from exc
    if value <= 0:
        raise ValueError(f"{name} harus lebih besar dari 0.")
    return value


FRONTEND_DIR = PROJECT_ROOT / "frontend"
DOWNLOAD_DIR = Path(os.getenv("TIKDROP_DOWNLOAD_DIR", str(PROJECT_ROOT / "downloads"))).expanduser().resolve()
TEMP_DIR = Path(os.getenv("TIKDROP_TEMP_DIR", str(PROJECT_ROOT / ".tmp"))).expanduser().resolve()
MAX_FILE_SIZE_MB = _env_int("TIKDROP_MAX_FILE_SIZE_MB", 500)
MAX_DURATION_SECONDS = _duration_limit_seconds()
FILE_TTL_HOURS = _env_int("TIKDROP_FILE_TTL_HOURS", 6)
MAX_CONCURRENT_DOWNLOADS = _env_int("TIKDROP_MAX_CONCURRENT_DOWNLOADS", 2)
DOWNLOAD_TIMEOUT_SECONDS = _env_int("TIKDROP_SOCKET_TIMEOUT", 30)

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)
