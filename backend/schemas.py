from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _validate_tiktok_url(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Gunakan tautan HTTP/HTTPS dari TikTok.")
    if parsed.username or parsed.password:
        raise ValueError("Format tautan tidak valid.")
    if not (host == "tiktok.com" or host.endswith(".tiktok.com")):
        raise ValueError("Link harus berasal dari domain TikTok.")
    if not parsed.path or parsed.path == "/":
        raise ValueError("Tautan video TikTok tidak lengkap.")
    return value


class DownloadRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    url: str = Field(min_length=8, max_length=2048)
    format: Literal["video", "audio"] = "video"
    quality: Literal["best", "1080", "720", "480"] = "best"

    @field_validator("url")
    @classmethod
    def validate_tiktok_url(cls, value: str) -> str:
        return _validate_tiktok_url(value)

    @model_validator(mode="after")
    def normalize_audio_quality(self) -> "DownloadRequest":
        if self.format == "audio":
            self.quality = "best"
        return self


class PreviewRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    url: str = Field(min_length=8, max_length=2048)

    @field_validator("url")
    @classmethod
    def validate_tiktok_url(cls, value: str) -> str:
        return _validate_tiktok_url(value)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=1024)
