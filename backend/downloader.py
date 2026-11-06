from __future__ import annotations

import json
import logging
import math
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from backend.config import DOWNLOAD_DIR, DOWNLOAD_TIMEOUT_SECONDS, FFMPEG_PATH, FFPROBE_PATH, MAX_DURATION_SECONDS, MAX_FILE_SIZE_MB, TEMP_DIR
from backend.schemas import DownloadRequest
from backend.storage import register_active, release_active

logger = logging.getLogger("tikdrop.downloader")


class MediaDownloadError(Exception):
    """A safe, user-facing download error."""


def _known_size(fmt: dict[str, Any] | None) -> int | None:
    if not fmt:
        return None
    value = fmt.get("filesize") or fmt.get("filesize_approx")
    try:
        parsed = int(value)
        return parsed if parsed >= 0 else None
    except (TypeError, ValueError):
        return None


def _check_known_size(size: int | None, limit_bytes: int) -> None:
    if size is not None and size > limit_bytes:
        raise MediaDownloadError(f"Ukuran sumber melebihi batas {MAX_FILE_SIZE_MB} MB. Pilih kualitas lebih rendah atau Auto.")


def _validate_actual_size(size_bytes: int) -> None:
    if size_bytes > MAX_FILE_SIZE_MB * 1024 * 1024:
        raise MediaDownloadError(f"File hasil melebihi batas ukuran {MAX_FILE_SIZE_MB} MB yang diizinkan.")


def _size_limit_progress_hook(limit_bytes: int, download_error_type):
    """Abort a yt-dlp transfer as soon as observed bytes exceed the cap."""
    def check(progress: dict[str, Any]) -> None:
        if progress.get("status") != "downloading":
            return
        amounts = [progress.get("downloaded_bytes"), progress.get("total_bytes"), progress.get("total_bytes_estimate")]
        for amount in amounts:
            try:
                if amount is not None and int(amount) > limit_bytes:
                    raise download_error_type("TIKDROP_SIZE_LIMIT_EXCEEDED")
            except (TypeError, ValueError):
                continue
    return check


def _validate_video_duration(duration: Any, source: str) -> None:
    try:
        seconds = float(duration)
    except (TypeError, ValueError):
        return
    if not math.isfinite(seconds) or seconds < 0:
        raise MediaDownloadError(f"Metadata durasi {source} tidak valid.")
    if seconds > MAX_DURATION_SECONDS:
        raise MediaDownloadError(
            f"Durasi video ({seconds:.1f} detik) melebihi batas {MAX_DURATION_SECONDS} detik."
        )


def _safe_url(url: str) -> str:
    # Do not log paths or queries: short-link paths and query strings can be tokens.
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.hostname or ''}"


def _run_version(executable: str) -> bool:
    try:
        subprocess.run([executable, "-version"], capture_output=True, text=True, check=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _media_tools() -> tuple[str | None, str | None]:
    ffmpeg = FFMPEG_PATH or shutil.which("ffmpeg")
    ffprobe = FFPROBE_PATH or shutil.which("ffprobe")
    if not ffmpeg or not _run_version(ffmpeg):
        raise MediaDownloadError("FFmpeg tidak tersedia atau tidak dapat dijalankan. Periksa instalasi FFmpeg lalu jalankan ulang TikDrop.")
    if not ffprobe or not _run_version(ffprobe):
        raise MediaDownloadError("FFprobe tidak tersedia atau tidak dapat dijalankan. Pastikan ffprobe.exe berada di folder bin FFmpeg.")
    return ffmpeg, ffprobe


def _probe(media_file: Path) -> dict[str, Any]:
    _, ffprobe = _media_tools()
    completed = subprocess.run([
        ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(media_file),
    ], capture_output=True, text=True, check=True, timeout=45)
    data = json.loads(completed.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    width, height = video.get("width"), video.get("height")
    fmt = data.get("format", {})
    try:
        fps_n, fps_d = (video.get("avg_frame_rate") or "0/1").split("/")
        fps = float(fps_n) / float(fps_d)
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    return {
        "actual_width": width, "actual_height": height,
        "actual_resolution": f"{width}x{height}" if width and height else None,
        "actual_fps": round(fps, 3) if fps else None,
        "actual_video_codec": video.get("codec_name"), "actual_audio_codec": audio.get("codec_name"),
        "actual_video_bitrate": int(video["bit_rate"]) if str(video.get("bit_rate", "")).isdigit() else None,
        "actual_audio_bitrate": int(audio["bit_rate"]) if str(audio.get("bit_rate", "")).isdigit() else None,
        "duration": float(fmt.get("duration", 0)) or None,
        "total_bitrate": int(fmt["bit_rate"]) if str(fmt.get("bit_rate", "")).isdigit() else None,
        "filesize": media_file.stat().st_size, "file_extension": media_file.suffix.lstrip("."),
        "has_video": bool(video), "has_audio": bool(audio),
    }


def _resolution_label(width: int | None, height: int | None) -> str | None:
    if not width or not height:
        return None
    return f"{min(width, height)}p"


def _video_formats(info: dict[str, Any]) -> list[dict[str, Any]]:
    formats = []
    for fmt in info.get("formats", []):
        if not isinstance(fmt, dict) or fmt.get("vcodec") in (None, "none"):
            continue
        width, height = fmt.get("width"), fmt.get("height")
        if not width or not height:
            continue
        formats.append(fmt)
    return formats


def _rank(fmt: dict[str, Any]) -> tuple[float, float, float, float]:
    dim = min(int(fmt.get("width") or 0), int(fmt.get("height") or 0))
    # Prefer an independently measured video bitrate when available. Codec is
    # a tie-breaker only: resolution/bitrate remain the main quality signals.
    video_rate = float(fmt.get("vbr") or fmt.get("tbr") or 0)
    codec = str(fmt.get("vcodec") or "").lower()
    codec_score = 1.0 if codec.startswith(("av01", "vp9", "vp09", "hev", "h265")) else 0.0
    return (dim, video_rate, codec_score, float(fmt.get("fps") or 0), float(fmt.get("filesize") or fmt.get("filesize_approx") or 0))


def _select_video(info: dict[str, Any], quality: str) -> dict[str, Any]:
    formats = _video_formats(info)
    if not formats:
        raise MediaDownloadError("Sumber tidak menyediakan format video yang dapat diproses.")
    if quality in {"best", "1080"}:
        return max(formats, key=_rank)
    cap = int(quality)
    eligible = [f for f in formats if min(int(f["width"]), int(f["height"])) <= cap]
    # Selecting the best source above the target is correct when every source
    # exceeds the target: FFmpeg will downscale it without transcoding variants.
    return max(eligible or formats, key=_rank)


def _format_report(formats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a complete, JSON-safe diagnostic inventory of video formats."""
    fields = ("format_id", "width", "height", "resolution", "fps", "vcodec", "acodec",
              "tbr", "vbr", "filesize", "filesize_approx", "ext")
    return [{key: fmt.get(key) for key in fields} for fmt in sorted(formats, key=_rank, reverse=True)]


def _scale_video(media_file: Path, target: int, ffmpeg: str) -> Path:
    meta = _probe(media_file)
    width, height = meta["actual_width"], meta["actual_height"]
    if not width or not height or min(width, height) <= target:
        return media_file
    ratio = min(1.0, target / min(width, height))
    out_w = min(width, max(2, int(round(width * ratio / 2) * 2)))
    out_h = min(height, max(2, int(round(height * ratio / 2) * 2)))
    output = media_file.with_name("processed.mp4")
    # CRF 18 proved wasteful for small TikTok sources. CRF 22 retained high
    # SSIM in the 480p integration sample while cutting output size materially.
    crf = {1080: "20", 720: "22", 480: "24"}[target]
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(media_file),
        "-map", "0:v:0", "-map", "0:a?", "-vf", f"scale={out_w}:{out_h}:flags=lanczos",
        "-c:v", "libx264", "-preset", "slow", "-crf", crf, "-pix_fmt", "yuv420p",
        "-c:a", "copy", "-movflags", "+faststart", "-map_metadata", "0", str(output)]
    logger.info("FFmpeg downscale %sx%s -> %sx%s (target=%sp)", width, height, out_w, out_h, target)
    try:
        subprocess.run(command, capture_output=True, text=True, check=True, timeout=max(DOWNLOAD_TIMEOUT_SECONDS * 20, 300))
    except subprocess.TimeoutExpired as exc:
        output.unlink(missing_ok=True)
        raise MediaDownloadError("Proses encoding FFmpeg terlalu lama. Coba kualitas Auto.") from exc
    except subprocess.CalledProcessError as exc:
        output.unlink(missing_ok=True)
        logger.error("FFmpeg gagal: %s", (exc.stderr or "")[-500:])
        raise MediaDownloadError("FFmpeg gagal mengubah resolusi video. Pastikan build mendukung libx264.") from exc
    if not output.is_file() or not output.stat().st_size:
        raise MediaDownloadError("FFmpeg tidak menghasilkan file video yang valid.")
    media_file.unlink(missing_ok=True)
    return output


def _friendly_error(error: Exception) -> str:
    raw = str(error).lower()
    if "tikdrop_size_limit_exceeded" in raw:
        return f"Ukuran download melebihi batas {MAX_FILE_SIZE_MB} MB. Pilih kualitas lebih rendah atau Auto."
    if "requested format is not available" in raw or "format is not available" in raw:
        return "Format video dari sumber tidak tersedia. Coba pilih Auto atau gunakan tautan video lain."
    if any(term in raw for term in ("private", "login", "sign in", "not available", "unavailable")):
        return "Video tidak bisa diakses. Pastikan videonya publik dan tautannya benar."
    if any(term in raw for term in ("http error 403", "http error 429", "too many requests", "blocked")):
        return "TikTok sedang membatasi permintaan. Coba lagi beberapa saat."
    if "unsupported url" in raw or "no suitable extractor" in raw:
        return "Tautan ini tidak dikenali sebagai video TikTok yang didukung."
    return "Video gagal diproses. Pastikan link benar dan video dapat diakses, lalu coba lagi."


def download_media(request: DownloadRequest) -> dict[str, Any]:
    """Inspect source formats, download the best fit, process, then probe final output."""
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError as YtDlpDownloadError
    except ImportError as exc:
        raise MediaDownloadError("Dependensi yt-dlp belum terpasang. Jalankan setup.bat terlebih dahulu.") from exc

    ffmpeg, _ = _media_tools()
    job_id = uuid.uuid4().hex
    job_dir = TEMP_DIR / job_id
    register_active(job_id)
    try:
        job_dir.mkdir(parents=True, exist_ok=False)
    except Exception:
        release_active(job_id)
        raise
    size_limit_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
    options: dict[str, Any] = {
        "outtmpl": str(job_dir / "source.%(ext)s"), "noplaylist": True, "quiet": True,
        "no_warnings": True, "ignoreerrors": False, "retries": 2, "fragment_retries": 2,
        "socket_timeout": DOWNLOAD_TIMEOUT_SECONDS, "max_filesize": size_limit_bytes,
        "restrictfilenames": True, "windowsfilenames": True, "overwrites": False,
        "cachedir": False, "writethumbnail": False, "writeinfojson": False, "continuedl": True,
        "noprogress": True,
        "progress_hooks": [_size_limit_progress_hook(size_limit_bytes, YtDlpDownloadError)],
        "ffmpeg_location": str(Path(ffmpeg).parent),
    }
    if request.format == "audio":
        options.update({"format": "bestaudio/best", "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]})
    try:
        with YoutubeDL(options) as ydl:
            logger.info("URL tervalidasi: %s", _safe_url(request.url))
            info = ydl.extract_info(request.url, download=False)
            if not isinstance(info, dict):
                raise MediaDownloadError("TikTok tidak mengembalikan metadata video yang dapat diproses.")
            if info.get("_type") in {"playlist", "multi_video"} or info.get("entries"):
                raise MediaDownloadError("Tautan harus mengarah ke satu video, bukan daftar video atau profil.")
            extractor = str(info.get("extractor_key") or info.get("extractor") or "").lower()
            if "tiktok" not in extractor:
                raise MediaDownloadError("Tautan tidak berhasil dikenali sebagai konten TikTok.")

            if request.format == "video":
                _validate_video_duration(info.get("duration"), "sumber")

            selected: dict[str, Any] | None = None
            source: dict[str, Any] | None = None
            if request.format == "video":
                formats = _video_formats(info)
                report = _format_report(formats)
                logger.info("Format sumber terdeteksi (%d): %s", len(formats), json.dumps(report, ensure_ascii=False))
                if not formats:
                    raise MediaDownloadError("Sumber tidak menyediakan format video yang dapat diproses.")
                selected = _select_video(info, request.quality)
                selected_size = _known_size(selected)
                _check_known_size(selected_size, size_limit_bytes)
                if selected.get("acodec") in (None, "none"):
                    audio_formats = [f for f in info.get("formats", []) if f.get("acodec") not in (None, "none")]
                    best_audio = max(audio_formats, key=lambda f: (float(f.get("abr") or f.get("tbr") or 0), float(f.get("filesize") or 0))) if audio_formats else None
                    audio_size = _known_size(best_audio)
                    _check_known_size(audio_size, size_limit_bytes)
                    if selected_size is not None and audio_size is not None:
                        _check_known_size(selected_size + audio_size, size_limit_bytes)
                source = max(formats, key=_rank)
                logger.info("Format video terpilih id=%s, source-best=%s, resolusi=%sx%s, target=%s",
                    selected.get("format_id"), source.get("format_id"), selected.get("width"), selected.get("height"), request.quality)
                # Select the chosen video rendition exactly; choose best audio
                # separately when the video is video-only. Avoid a broad /best
                # fallback that can silently change the selected video stream.
                options["format"] = f"{selected['format_id']}+bestaudio/{selected['format_id']}"
                options["merge_output_format"] = "mp4"
                options["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]
            else:
                audio_formats = [f for f in info.get("formats", []) if f.get("acodec") not in (None, "none")]
                best_audio = max(audio_formats, key=lambda f: (float(f.get("abr") or f.get("tbr") or 0), float(f.get("filesize") or 0))) if audio_formats else None
                _check_known_size(_known_size(best_audio), size_limit_bytes)
                logger.info("Format audio terpilih id=%s", best_audio.get("format_id") if best_audio else "best")

            if request.format == "audio":
                ydl.process_ie_result(info, download=True)
            else:
                ydl.download([request.url])
            # ydl.download returns a code; info remains the metadata for the item.
        candidates = [p for p in job_dir.iterdir() if p.is_file() and not p.name.lower().endswith((".part", ".ytdl", ".json", ".jpg", ".webp"))]
        if not candidates:
            raise MediaDownloadError("File hasil download tidak ditemukan.")
        source_file = max(candidates, key=lambda p: p.stat().st_mtime)
        if request.format == "audio":
            if source_file.suffix.lower() != ".mp3":
                raise MediaDownloadError("Konversi MP3 belum berhasil. Pastikan FFmpeg mendukung libmp3lame.")
            actual = _probe(source_file)
            if actual["has_video"] or not actual["has_audio"]:
                raise MediaDownloadError("File MP3 hasil proses memiliki stream yang tidak valid.")
            requested = "MP3"
            source_dims = None
        else:
            if source_file.suffix.lower() != ".mp4":
                raise MediaDownloadError("Konversi MP4 belum berhasil. Pastikan FFmpeg terdeteksi.")
            source_dims = _probe(source_file)
            # 1080p is source-best: retain the original stream whenever it is
            # at or below 1080p; only transcode if its short edge exceeds 1080.
            if request.quality in {"720", "480"} or (request.quality == "1080" and min(source_dims["actual_width"] or 0, source_dims["actual_height"] or 0) > 1080):
                source_file = _scale_video(source_file, int(request.quality), ffmpeg)
            actual = _probe(source_file)
            if not actual["has_video"]:
                raise MediaDownloadError("File hasil tidak memiliki stream video.")
            if not actual["has_audio"]:
                raise MediaDownloadError("Audio tidak ditemukan pada hasil video yang diproses.")
            _validate_video_duration(actual["duration"], "hasil akhir")
            requested = "Auto" if request.quality == "best" else f"{request.quality}p"

        _validate_actual_size(actual["filesize"])

        logger.info("Resolusi aktual=%s, filesize=%d, download selesai", actual["actual_resolution"], actual["filesize"])
        final_name = f"{job_id}.{source_file.suffix.lower().lstrip('.')}"
        final_path = DOWNLOAD_DIR / final_name
        shutil.move(str(source_file), str(final_path))
        result: dict[str, Any] = {
            "download_url": f"/api/files/{final_name}", "filename": final_name,
            "title": str(info.get("title") or "Video TikTok").strip()[:180] or "Video TikTok",
            "requested_quality": requested, "source_resolution": _resolution_label(source_dims.get("actual_width"), source_dims.get("actual_height")) if source_dims else None,
            "resolution": actual["actual_resolution"], "actual_quality": _resolution_label(actual["actual_width"], actual["actual_height"]),
            "actual_width": actual["actual_width"], "actual_height": actual["actual_height"],
            "actual_fps": actual["actual_fps"], "actual_video_codec": actual["actual_video_codec"],
            "actual_audio_codec": actual["actual_audio_codec"], "actual_video_bitrate": actual["actual_video_bitrate"],
            "actual_audio_bitrate": actual["actual_audio_bitrate"], "duration": actual["duration"],
            "total_bitrate": actual["total_bitrate"], "filesize": actual["filesize"], "file_extension": actual["file_extension"],
            "source_formats": report if request.format == "video" else [],
            "source_below_requested": bool(request.format == "video" and request.quality != "best" and source_dims and min(source_dims["actual_width"], source_dims["actual_height"]) < int(request.quality)),
            # Stable concise field names for API clients; values still come
            # from the selected source probe or the completed output probe.
            "source_width": source_dims.get("actual_width") if source_dims else None,
            "source_height": source_dims.get("actual_height") if source_dims else None,
            "video_codec": actual["actual_video_codec"],
            "audio_codec": actual["actual_audio_codec"],
            "video_bitrate": actual["actual_video_bitrate"],
            "audio_bitrate": actual["actual_audio_bitrate"],
            "fps": actual["actual_fps"],
        }
        safe_title = result["title"].encode("ascii", "backslashreplace").decode("ascii")
        logger.info("Hasil tersimpan title=%s requested=%s source=%s output=%s size=%d",
            safe_title, requested, result["source_resolution"], actual["actual_resolution"], actual["filesize"])
        return result
    except MediaDownloadError:
        raise
    except YtDlpDownloadError as exc:
        logger.warning("yt-dlp gagal memproses %s (jenis=%s)", _safe_url(request.url), type(exc).__name__)
        raise MediaDownloadError(_friendly_error(exc)) from exc
    except Exception as exc:
        logger.error("Kesalahan internal pada %s (jenis=%s)", _safe_url(request.url), type(exc).__name__)
        raise MediaDownloadError(_friendly_error(exc)) from exc
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
        release_active(job_id)
