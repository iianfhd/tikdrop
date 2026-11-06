from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import math
import re
import shutil
import sys
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from backend.config import (
    DOWNLOAD_DIR,
    FILE_TTL_HOURS,
    FFMPEG_PATH,
    FFPROBE_PATH,
    FRONTEND_DIR,
    MAX_CONCURRENT_DOWNLOADS,
    MAX_DOWNLOADS_PER_IP_PER_HOUR,
    MAX_FILE_SERVES_PER_IP_PER_HOUR,
    TEMP_DIR,
)
from backend.downloader import MediaDownloadError, download_media
from backend.schemas import DownloadRequest, PreviewRequest
from backend.storage import acquire_download_file, cleanup_expired, release_active

# Windows consoles may use legacy code pages. Make console writes UTF-8 safe;
# unsupported glyphs are escaped so logging can never fail a completed job.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError, ValueError):
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("tikdrop")
download_slots = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
preview_slots = asyncio.Semaphore(2)
preview_lock = asyncio.Lock()
preview_cache: dict[str, tuple[float, dict[str, object]]] = {}
preview_inflight: dict[str, asyncio.Future] = {}
PREVIEW_CACHE_SECONDS = 60
PREVIEW_CACHE_MAX = 256
PREVIEW_RATE_WINDOW_SECONDS = 60
PREVIEW_RATE_MAX_REQUESTS = 30
PREVIEW_RATE_MAX_CLIENTS = 1024
preview_rate: dict[str, list[float]] = {}
DOWNLOAD_RATE_WINDOW_SECONDS = 60 * 60
DOWNLOAD_RATE_MAX_CLIENTS = 4096
download_rate: dict[str, list[float]] = {}
download_rate_lock = asyncio.Lock()
file_serve_rate: dict[str, list[float]] = {}
file_serve_rate_lock = asyncio.Lock()
FILE_NAME_RE = re.compile(r"^[a-f0-9]{32}\.(?:mp4|mp3)$")

def _client_ip(request: Request) -> str:
    """Use the direct peer by default; trust Cloudflare's address only behind local cloudflared."""
    peer = request.client.host if request.client else "unknown"
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if peer_ip.is_loopback:
        forwarded = request.headers.get("cf-connecting-ip", "").strip()
        try:
            return str(ipaddress.ip_address(forwarded)) if forwarded else str(peer_ip)
        except ValueError:
            pass
    return str(peer_ip)


async def _allow_download(client: str) -> tuple[bool, int]:
    now = time.monotonic()
    async with download_rate_lock:
        attempts = [stamp for stamp in download_rate.get(client, []) if now - stamp < DOWNLOAD_RATE_WINDOW_SECONDS]
        if len(attempts) >= MAX_DOWNLOADS_PER_IP_PER_HOUR:
            download_rate[client] = attempts
            retry_after = max(1, int(DOWNLOAD_RATE_WINDOW_SECONDS - (now - attempts[0])))
            return False, retry_after
        attempts.append(now)
        download_rate[client] = attempts
        if len(download_rate) > DOWNLOAD_RATE_MAX_CLIENTS:
            oldest = min(download_rate, key=lambda key: download_rate[key][-1] if download_rate[key] else 0)
            download_rate.pop(oldest, None)
        return True, 0


def _preview_key(url: str) -> str:
    parsed = urlsplit(url)
    canonical = urlunsplit((parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.path, "", ""))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _extract_preview(url: str) -> dict[str, object]:
    """Read public TikTok metadata only; this path never starts a media download."""
    from yt_dlp import YoutubeDL
    from backend.downloader import _safe_url, _friendly_error

    class QuietYtDlpLogger:
        # yt-dlp error strings can contain signed URLs; API logs only safe host/type details.
        def debug(self, _: str) -> None: pass
        def info(self, _: str) -> None: pass
        def warning(self, _: str) -> None: pass
        def error(self, _: str) -> None: pass

    options = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True,
               "cachedir": False, "socket_timeout": 15, "extractor_retries": 1, "logger": QuietYtDlpLogger()}
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        logger.info("Preview metadata gagal (host=%s, jenis=%s)", _safe_url(url), type(exc).__name__)
        raise ValueError(_friendly_error(exc)) from exc
    if not isinstance(info, dict) or info.get("_type") in {"playlist", "multi_video"} or info.get("entries"):
        raise ValueError("Tautan harus mengarah ke satu video TikTok yang dapat diakses.")
    extractor = str(info.get("extractor_key") or info.get("extractor") or "").lower()
    if "tiktok" not in extractor:
        raise ValueError("Tautan tidak berhasil dikenali sebagai konten TikTok.")

    def clean(value: object, limit: int) -> str | None:
        if not isinstance(value, str):
            return None
        value = "".join(ch for ch in value if ch >= " " and ch != "\x7f").strip()
        return value[:limit] or None

    allowed_suffixes = ("tiktokcdn.com", "tiktokcdn-us.com", "tiktokv.com", "byteoversea.com", "ibytedtos.com")
    candidates = []
    for thumb in info.get("thumbnails") or []:
        if not isinstance(thumb, dict) or not isinstance(thumb.get("url"), str):
            continue
        candidate = thumb["url"]
        parsed = urlsplit(candidate)
        host = (parsed.hostname or "").lower().rstrip(".")
        try:
            safe_port = parsed.port in (None, 443)
        except ValueError:
            safe_port = False
        if parsed.scheme != "https" or not safe_port or not any(host == suffix or host.endswith("." + suffix) for suffix in allowed_suffixes):
            continue
        try:
            width, height = int(thumb.get("width") or 0), int(thumb.get("height") or 0)
        except (TypeError, ValueError):
            width, height = 0, 0
        thumb_id = str(thumb.get("id") or "").lower()
        try:
            preference = int(thumb.get("preference") or 0)
        except (TypeError, ValueError):
            preference = 0
        rank = (width * height, 2 if "origin" in thumb_id else 1 if "cover" in thumb_id else 0, preference)
        candidates.append((rank, candidate, width or None, height or None))
    candidates.sort(key=lambda row: row[0], reverse=True)
    if not candidates and isinstance(info.get("thumbnail"), str):
        candidate = info["thumbnail"]
        parsed = urlsplit(candidate)
        host = (parsed.hostname or "").lower().rstrip(".")
        try:
            safe_port = parsed.port in (None, 443)
        except ValueError:
            safe_port = False
        if parsed.scheme == "https" and safe_port and any(host == suffix or host.endswith("." + suffix) for suffix in allowed_suffixes):
            candidates.append(((0, 0, 0), candidate, None, None))
    urls = list(dict.fromkeys(row[1] for row in candidates))
    chosen = candidates[0] if candidates else None
    return {
        "status": "success", "title": clean(info.get("title"), 180),
        "author": clean(info.get("uploader") or info.get("creator") or info.get("channel"), 100),
        "duration": info.get("duration") if isinstance(info.get("duration"), (int, float)) and math.isfinite(info.get("duration")) else None,
        "thumbnail_url": chosen[1] if chosen else None,
        "thumbnail_fallbacks": urls[1:],
        "thumbnail_width": chosen[2] if chosen else None,
        "thumbnail_height": chosen[3] if chosen else None,
    }


def cleanup_old_files() -> None:
    cutoff = time.time() - (FILE_TTL_HOURS * 60 * 60)
    try:
        cleanup_expired(DOWNLOAD_DIR, TEMP_DIR, cutoff, logger)
    except Exception as exc:
        # Cleanup is best-effort and must never interrupt the API worker.
        logger.warning("Cleanup TikDrop gagal (jenis=%s)", type(exc).__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_old_files()
    yield


app = FastAPI(
    title="TikDrop API",
    version="1.1.0",
    description="Backend lokal TikDrop untuk memproses satu video TikTok yang dapat diakses.",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


class ActiveFileResponse(FileResponse):
    def __init__(self, *args, active_id: str, **kwargs):
        super().__init__(*args, **kwargs)
        self._active_id = active_id

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            release_active(self._active_id)
            download_slots.release()


@app.exception_handler(RequestValidationError)
async def validation_error_response(_: Request, exc: RequestValidationError) -> JSONResponse:
    """Return a concise validation error without echoing submitted URLs."""
    errors = exc.errors()
    message = str(errors[0].get("msg", "Permintaan tidak valid.")) if errors else "Permintaan tidak valid."
    if message.startswith("Value error, "):
        message = message.removeprefix("Value error, ")
    return JSONResponse(status_code=422, content={"detail": message})


@app.get("/api/health")
async def health() -> dict[str, object]:
    from backend.downloader import _run_version
    ffmpeg = FFMPEG_PATH or shutil.which("ffmpeg")
    ffprobe = FFPROBE_PATH or shutil.which("ffprobe")
    return {
        "status": "ok",
        "ffmpeg_available": bool(ffmpeg and _run_version(ffmpeg)),
        "ffprobe_available": bool(ffprobe and _run_version(ffprobe)),
        "frontend_available": FRONTEND_DIR.is_dir() and (FRONTEND_DIR / "index.html").is_file(),
    }


@app.get("/manifest.json", include_in_schema=False)
async def pwa_manifest() -> FileResponse:
    return FileResponse(
        FRONTEND_DIR / "manifest.json",
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
    )


@app.get("/sw.js", include_in_schema=False)
async def pwa_service_worker() -> FileResponse:
    return FileResponse(
        FRONTEND_DIR / "sw.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
    )


@app.post("/api/preview")
async def preview_video(payload: PreviewRequest, request: Request) -> dict[str, object]:
    """Return TikTok metadata and original thumbnail URLs without downloading video."""
    key = _preview_key(payload.url)
    owner = False
    async with preview_lock:
        now = time.monotonic()
        client = _client_ip(request)
        recent = [stamp for stamp in preview_rate.get(client, []) if now - stamp < PREVIEW_RATE_WINDOW_SECONDS]
        if len(recent) >= PREVIEW_RATE_MAX_REQUESTS:
            preview_rate[client] = recent
            raise HTTPException(status_code=429, detail="Terlalu banyak permintaan pratinjau. Tunggu sebentar lalu coba lagi.")
        recent.append(now)
        preview_rate[client] = recent
        if len(preview_rate) > PREVIEW_RATE_MAX_CLIENTS:
            oldest_client = min(preview_rate, key=lambda item: preview_rate[item][-1] if preview_rate[item] else 0)
            preview_rate.pop(oldest_client, None)
        expired = [item for item, (created, _) in preview_cache.items() if now - created >= PREVIEW_CACHE_SECONDS]
        for item in expired:
            preview_cache.pop(item, None)
        cached = preview_cache.get(key)
        if cached is not None:
            return dict(cached[1])
        future = preview_inflight.get(key)
        if future is None:
            future = asyncio.get_running_loop().create_future()
            preview_inflight[key] = future
            owner = True
    if not owner:
        result = await asyncio.shield(future)
    else:
        try:
            async with preview_slots:
                result = await run_in_threadpool(_extract_preview, payload.url)
        except ValueError as exc:
            result = {"_error": str(exc)}
        except asyncio.CancelledError:
            async with preview_lock:
                if not future.done():
                    future.set_result({"_error": "Pratinjau dibatalkan. Coba lagi."})
                preview_inflight.pop(key, None)
            raise
        except Exception as exc:
            logger.error("Preview API gagal (jenis=%s)", type(exc).__name__)
            result = {"_error": "Pratinjau tidak tersedia. Anda tetap bisa mencoba download."}
        async with preview_lock:
            if not result.get("_error"):
                if len(preview_cache) >= PREVIEW_CACHE_MAX:
                    oldest = min(preview_cache, key=lambda item: preview_cache[item][0])
                    preview_cache.pop(oldest, None)
                preview_cache[key] = (time.monotonic(), result)
            if not future.done():
                future.set_result(result)
            preview_inflight.pop(key, None)
    if result.get("_error"):
        raise HTTPException(status_code=422, detail=result["_error"])
    return dict(result)


@app.post("/api/download")
async def create_download(payload: DownloadRequest, request: Request) -> dict[str, object]:
    client = _client_ip(request)
    allowed, retry_after = await _allow_download(client)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Batas download per alamat IP tercapai. Coba lagi setelah satu jam.",
            headers={"Retry-After": str(retry_after)},
        )
    cleanup_old_files()
    async with download_slots:
        try:
            result = await run_in_threadpool(download_media, payload)
            return result
        except MediaDownloadError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            logger.error("Kesalahan API yang tidak tertangani (jenis=%s)", type(exc).__name__)
            raise HTTPException(status_code=500, detail="Terjadi kesalahan internal. Coba lagi.") from exc


@app.get("/api/files/{filename}")
async def get_download(filename: str, request: Request) -> FileResponse:
    if not FILE_NAME_RE.fullmatch(filename):
        raise HTTPException(status_code=404, detail="File tidak ditemukan atau tautan tidak valid.")
    client = _client_ip(request)
    now = time.monotonic()
    async with file_serve_rate_lock:
        recent = [stamp for stamp in file_serve_rate.get(client, []) if now - stamp < DOWNLOAD_RATE_WINDOW_SECONDS]
        if len(recent) >= MAX_FILE_SERVES_PER_IP_PER_HOUR:
            file_serve_rate[client] = recent
            retry_after = max(1, int(DOWNLOAD_RATE_WINDOW_SECONDS - (now - recent[0])))
            raise HTTPException(
                status_code=429,
                detail="Batas pengambilan file per alamat IP tercapai. Coba lagi nanti.",
                headers={"Retry-After": str(retry_after)},
            )
        recent.append(now)
        file_serve_rate[client] = recent
        if len(file_serve_rate) > DOWNLOAD_RATE_MAX_CLIENTS:
            oldest = min(file_serve_rate, key=lambda key: file_serve_rate[key][-1] if file_serve_rate[key] else 0)
            file_serve_rate.pop(oldest, None)

    await download_slots.acquire()
    try:
        path = acquire_download_file(filename)
    except Exception:
        download_slots.release()
        raise
    if path is None:
        download_slots.release()
        raise HTTPException(status_code=404, detail="File sudah tidak tersedia. Silakan unduh ulang.")
    media_type = "audio/mpeg" if path.suffix == ".mp3" else "video/mp4"
    try:
        return ActiveFileResponse(
            path,
            active_id=filename.rsplit(".", 1)[0],
            media_type=media_type,
            filename=filename,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
    except Exception:
        release_active(filename.rsplit(".", 1)[0])
        download_slots.release()
        raise


# API routes are registered before the frontend mount so /api/* is not shadowed.
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
