from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import logging
import math
import re
import secrets
import shutil
import sys
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from backend.config import (
    DOWNLOAD_DIR,
    ACCESS_PASSWORD,
    FILE_TTL_HOURS,
    FFMPEG_PATH,
    FFPROBE_PATH,
    FRONTEND_DIR,
    MAX_CONCURRENT_DOWNLOADS,
    TEMP_DIR,
)
from backend.downloader import MediaDownloadError, download_media
from backend.schemas import DownloadRequest, LoginRequest, PreviewRequest
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
FILE_NAME_RE = re.compile(r"^[a-f0-9]{32}\.(?:mp4|mp3)$")
SESSION_COOKIE = "tikdrop_session"
SESSION_TTL_SECONDS = 8 * 60 * 60
LOGIN_RATE_WINDOW_SECONDS = 5 * 60
LOGIN_RATE_MAX_ATTEMPTS = 8
LOGIN_RATE_MAX_CLIENTS = 1024
login_attempts: dict[str, list[float]] = {}
login_attempt_lock = asyncio.Lock()


def _session_signature(payload: str) -> str:
    key = hashlib.sha256(b"tikdrop-session-v1\0" + ACCESS_PASSWORD.encode("utf-8")).digest()
    digest = hmac.new(key, payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _create_session_token() -> str:
    payload = f"v1.{int(time.time())}.{secrets.token_urlsafe(18)}"
    return f"{payload}.{_session_signature(payload)}"


def _valid_session_token(token: str | None) -> bool:
    if not ACCESS_PASSWORD or not token:
        return False
    try:
        version, timestamp, nonce, signature = token.split(".", 3)
        issued_at = int(timestamp)
        payload = f"{version}.{timestamp}.{nonce}"
        age = time.time() - issued_at
        return (
            version == "v1" and 0 <= age <= SESSION_TTL_SECONDS and
            hmac.compare_digest(signature, _session_signature(payload))
        )
    except (ValueError, TypeError):
        return False


def _request_is_https(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    if forwarded == "https" or request.url.scheme == "https":
        return True
    cf_visitor = request.headers.get("cf-visitor", "").lower().replace(" ", "")
    return '"scheme":"https"' in cf_visitor


def _is_local_http_origin(request: Request) -> bool:
    host = request.url.hostname or ""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _transport_is_allowed(request: Request) -> bool:
    return _request_is_https(request) or _is_local_http_origin(request)


def _same_origin_request(request: Request) -> bool:
    fetch_site = request.headers.get("sec-fetch-site", "").lower()
    if fetch_site and fetch_site not in {"same-origin", "none"}:
        return False
    origin = request.headers.get("origin")
    if not origin:
        return fetch_site in {"", "same-origin", "none"}
    try:
        parsed = urlsplit(origin)
        if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            return False
        origin_host = parsed.netloc.lower()
        request_hosts = {request.headers.get("host", "").lower()}
        forwarded_host = request.headers.get("x-forwarded-host", "").split(",", 1)[0].strip().lower()
        if forwarded_host:
            request_hosts.add(forwarded_host)
        expected_scheme = "https" if _request_is_https(request) else "http"
        if fetch_site == "same-origin":
            return parsed.scheme.lower() == expected_scheme
        return parsed.scheme.lower() == expected_scheme and origin_host in request_hosts
    except ValueError:
        return False


async def _check_login_rate_limit(client: str) -> bool:
    now = time.monotonic()
    async with login_attempt_lock:
        attempts = [stamp for stamp in login_attempts.get(client, []) if now - stamp < LOGIN_RATE_WINDOW_SECONDS]
        if len(attempts) >= LOGIN_RATE_MAX_ATTEMPTS:
            login_attempts[client] = attempts
            return False
        attempts.append(now)
        login_attempts[client] = attempts
        if len(login_attempts) > LOGIN_RATE_MAX_CLIENTS:
            oldest = min(login_attempts, key=lambda key: login_attempts[key][-1] if login_attempts[key] else 0)
            login_attempts.pop(oldest, None)
        return True


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
    if not ACCESS_PASSWORD:
        raise RuntimeError("TIKDROP_ACCESS_PASSWORD belum diatur. TikDrop tidak akan berjalan tanpa password akses.")
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_old_files()
    yield


app = FastAPI(
    title="TikDrop API",
    version="1.1.0",
    description="Backend lokal TikDrop untuk memproses satu video TikTok yang dapat diakses.",
    lifespan=lifespan,
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


@app.exception_handler(RequestValidationError)
async def validation_error_response(_: Request, exc: RequestValidationError) -> JSONResponse:
    """Return a concise validation error without echoing submitted URLs."""
    errors = exc.errors()
    message = str(errors[0].get("msg", "Permintaan tidak valid.")) if errors else "Permintaan tidak valid."
    if message.startswith("Value error, "):
        message = message.removeprefix("Value error, ")
    return JSONResponse(status_code=422, content={"detail": message})


@app.middleware("http")
async def require_access_password(request: Request, call_next):
    public_routes = {
        ("/login", "GET"), ("/login.css", "GET"), ("/login.js", "GET"),
        ("/api/auth/login", "POST"),
    }
    path = request.url.path
    if (path, request.method) in public_routes:
        if path == "/api/auth/login" and not _transport_is_allowed(request):
            return JSONResponse(status_code=403, content={"detail": "Login memerlukan HTTPS. HTTP hanya diizinkan pada localhost."}, headers={"Cache-Control": "no-store"})
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not _same_origin_request(request):
            return JSONResponse(status_code=403, content={"detail": "Permintaan lintas situs ditolak."}, headers={"Cache-Control": "no-store"})
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    if not _valid_session_token(request.cookies.get(SESSION_COOKIE)):
        if request.method in {"GET", "HEAD"} and path not in {"/login", "/login.css", "/login.js"} and "text/html" in request.headers.get("accept", ""):
            response = RedirectResponse("/login", status_code=303)
        else:
            response = JSONResponse(status_code=401, content={"detail": "Password akses diperlukan."})
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    if not _transport_is_allowed(request):
        return JSONResponse(status_code=403, content={"detail": "Gunakan HTTPS untuk mengakses TikDrop dari jaringan lain."}, headers={"Cache-Control": "no-store"})

    if not _same_origin_request(request):
        return JSONResponse(status_code=403, content={"detail": "Permintaan lintas situs ditolak."}, headers={"Cache-Control": "no-store"})

    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/login", include_in_schema=False)
async def login_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "login.html", media_type="text/html")


@app.get("/login.css", include_in_schema=False)
async def login_styles() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "login.css", media_type="text/css")


@app.get("/login.js", include_in_schema=False)
async def login_script() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "login.js", media_type="text/javascript")


@app.post("/api/auth/login")
async def create_session(payload: LoginRequest, request: Request) -> Response:
    client = request.client.host if request.client else "unknown"
    if not await _check_login_rate_limit(client):
        return JSONResponse(
            status_code=429,
            content={"detail": "Terlalu banyak percobaan. Tunggu beberapa menit lalu coba lagi."},
            headers={"Cache-Control": "no-store", "Retry-After": str(LOGIN_RATE_WINDOW_SECONDS)},
        )
    if not hmac.compare_digest(payload.password.encode("utf-8"), ACCESS_PASSWORD.encode("utf-8")):
        return JSONResponse(status_code=401, content={"detail": "Password akses salah."}, headers={"Cache-Control": "no-store"})
    async with login_attempt_lock:
        login_attempts.pop(client, None)
    response = JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})
    response.set_cookie(
        key=SESSION_COOKIE,
        value=_create_session_token(),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=_request_is_https(request),
        samesite="strict",
        path="/",
    )
    return response


@app.post("/api/auth/logout", status_code=204)
async def end_session(request: Request) -> Response:
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    response.delete_cookie(
        key=SESSION_COOKIE,
        path="/",
        secure=_request_is_https(request),
        httponly=True,
        samesite="strict",
    )
    return response


@app.get("/api/health")
async def health() -> dict[str, object]:
    from backend.downloader import _run_version
    ffmpeg = FFMPEG_PATH or shutil.which("ffmpeg")
    ffprobe = FFPROBE_PATH or shutil.which("ffprobe")
    return {
        "status": "ok",
        "ffmpeg_available": bool(ffmpeg and _run_version(ffmpeg)),
        "ffprobe_available": bool(ffprobe and _run_version(ffprobe)),
        "ffmpeg_path": ffmpeg,
        "ffprobe_path": ffprobe,
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
        client = request.client.host if request.client else "unknown"
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
async def create_download(payload: DownloadRequest) -> dict[str, object]:
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
async def get_download(filename: str) -> FileResponse:
    if not FILE_NAME_RE.fullmatch(filename):
        raise HTTPException(status_code=404, detail="File tidak ditemukan atau tautan tidak valid.")
    path = acquire_download_file(filename)
    if path is None:
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
        raise


# API routes are registered before the frontend mount so /api/* is not shadowed.
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
