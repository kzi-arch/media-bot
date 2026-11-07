"""Abstraksi dua mesin unduh.

yt-dlp    -> video (YouTube, TikTok, Reels, X video, FB video, 1800+ situs)
gallery-dl -> foto & carousel (Instagram post, Pinterest, X image, Threads)
"""
from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import gallery_dl
import yt_dlp

from config import CONFIG

# ─── domain yang punya perilaku khusus ────────────────────────────────
IMAGE_FIRST_HOSTS = {
    "instagram.com": "instagram",
    "pinterest.": "pinterest",
    "threads.net": "threads",
    "twitter.com": "x",
    "x.com": "x",
}
VIDEO_HOSTS = {
    "youtube.com", "youtu.be", "tiktok.com", "vimeo.com",
    "facebook.com", "fb.watch", "dailymotion.com", "twitch.tv",
    "reddit.com", "bilibili.com", "rumble.com", "streamable.com",
}
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


class DownloadError(RuntimeError):
    pass


@dataclass
class MediaInfo:
    url: str
    title: str
    platform: str
    has_video: bool
    has_image: bool
    duration: int | None = None
    thumb: str | None = None


# ─── probe: ekstrak metadata tanpa mengunduh ──────────────────────────
def _probe_sync(url: str) -> MediaInfo:
    platform = _platform_of(url)
    cookie = CONFIG.cookies.get(platform)

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 20,
    }
    if cookie:
        opts["cookiefile"] = str(cookie)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise DownloadError(_friendly(exc)) from exc

    if info is None:
        raise DownloadError("Tidak ada media yang bisa diekstrak dari URL ini.")

    # playlist / carousel
    entries = info.get("entries")
    if entries:
        entries = [e for e in entries if e]
        info = entries[0] if entries else info

    vcodec = info.get("vcodec")
    acodec = info.get("acodec")
    ext = (info.get("ext") or "").lower()

    has_video = bool(vcodec and vcodec != "none") or ext in {"mp4", "webm", "mkv", "mov"}
    has_image = ext in {"jpg", "jpeg", "png", "webp", "gif"} or (
        info.get("vcodec") == "none" and info.get("acodec") == "none"
    )

    # Instagram/Facebook kadang melaporkan image sebagai format tanpa codec
    if not has_image and not has_video:
        has_image = True

    return MediaInfo(
        url=url,
        title=(info.get("title") or info.get("description") or "media")[:200],
        platform=platform,
        has_video=has_video,
        has_image=has_image,
        duration=info.get("duration"),
        thumb=info.get("thumbnail"),
    )


# ─── download: dua mesin, satu antarmuka ──────────────────────────────
def _download_video_sync(url: str, outdir: Path) -> list[Path]:
    platform = _platform_of(url)
    cookie = CONFIG.cookies.get(platform)

    # format bertingkat: <=50MB dulu, lalu progresif, lalu audio
    max_b = CONFIG.max_upload_bytes
    fmt = (
        f"bestvideo[filesize<{max_b}]+bestaudio/"
        f"best[filesize<{max_b}]/"
        f"bestvideo[height<=720]+bestaudio/"
        f"best[height<=720]/best"
    )

    opts = {
        "outtmpl": str(outdir / "%(title).80s-%(id)s.%(ext)s"),
        "format": fmt,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 4,
        "restrictfilenames": True,
        "trim_file_name": 120,
    }
    if cookie:
        opts["cookiefile"] = str(cookie)

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    return sorted(outdir.glob("*"))


def _download_images_sync(url: str, outdir: Path) -> list[Path]:
    platform = _platform_of(url)
    cookie = CONFIG.cookies.get(platform)

    cfg = gallery_dl.config
    cfg.clear()
    cfg.set(("extractor",), "base-directory", str(outdir))
    cfg.set(("extractor",), "path", "")
    cfg.set(("extractor",), "filename", "{num:03d}_{filename}.{extension}")
    cfg.set(("extractor",), "image-range", "1-10")   # batasi carousel
    if cookie:
        cfg.set(("extractor", platform), "cookies", str(cookie))

    try:
        gallery_dl.job.DownloadJob(url).run()
    except Exception as exc:
        # fallback: beberapa platform tidak didukung gallery-dl, coba yt-dlp
        if "Unsupported URL" in str(exc):
            return _download_video_sync(url, outdir)
        raise

    files = [
        p for p in outdir.rglob("*")
        if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4"}
    ]
    return sorted(files)[:10]


async def download(url: str, mode: str, workdir: Path) -> list[Path]:
    """mode: 'video' | 'image' | 'both'"""
    if mode == "video":
        return await asyncio.to_thread(_download_video_sync, url, workdir)

    if mode == "image":
        return await asyncio.to_thread(_download_images_sync, url, workdir)

    # both: coba gambar dulu (murah), lalu video jika tidak ada hasil
    images = await asyncio.to_thread(_download_images_sync, url, workdir)
    if images:
        return images
    return await asyncio.to_thread(_download_video_sync, url, workdir)


async def probe(url: str) -> MediaInfo:
    return await asyncio.to_thread(_probe_sync, url)


# ─── util ─────────────────────────────────────────────────────────────
def _platform_of(url: str) -> str:
    host = re.sub(r"^https?://", "", url, flags=re.I).split("/")[0].lower()
    host = host.split(":")[0]
    for prefix in ("www.", "m.", "mobile."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host


def is_supported(url: str) -> bool:
    host = _platform_of(url)
    return any(h in host for h in IMAGE_FIRST_HOSTS) or any(
        host.endswith(h) or host == h for h in VIDEO_HOSTS
    ) or "instagram" in host or "tiktok" in host


def extract_url(text: str) -> str | None:
    m = URL_RE.search(text or "")
    return m.group(0).rstrip(".,)") if m else None


def _friendly(exc: Exception) -> str:
    msg = str(exc)
    lowered = msg.lower()
    if "login" in lowered or "cookies" in lowered:
        return "Konten ini butuh login. Tambahkan cookies platform ke folder cookies/."
    if "private" in lowered:
        return "Konten privat — tidak bisa diakses tanpa akun yang punya izin."
    if "unsupported url" in lowered:
        return "Platform ini belum didukung. Coba link video langsung."
    if "404" in msg or "not found" in lowered:
        return "Media tidak ditemukan. Link mungkin sudah dihapus."
    if "timed out" in lowered or "timeout" in lowered:
        return "Server sumber lambat merespons. Coba lagi."
    return msg[:300]


def cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)