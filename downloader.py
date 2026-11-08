"""Abstraksi dua mesin unduh.

yt-dlp     -> video (YouTube, TikTok, Reels, X video, FB video, 1800+ situs)
gallery-dl -> foto  (Instagram post, Pinterest, X image, Threads, 100+ situs)

Keduanya dijalankan sebagai subprocess: config tidak balapan antar job,
dan timeout benar-benar menghentikan proses.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import sys
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from config import CONFIG

log = logging.getLogger(__name__)

# ─── konstanta ────────────────────────────────────────────────────────
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
AUDIO_EXTS = {".m4a", ".mp3", ".opus", ".ogg", ".wav"}
MEDIA_EXTS = VIDEO_EXTS | IMAGE_EXTS | AUDIO_EXTS

# hostname -> kunci di CONFIG.cookies (config.py memakai nama pendek)
COOKIE_KEYS = {
    "instagram.com": "instagram",
    "x.com": "x",
    "twitter.com": "x",
    "facebook.com": "facebook",
    "fb.watch": "facebook",
    "pinterest.com": "pinterest",
    "pin.it": "pinterest",
    "threads.net": "threads",
}

# hostname -> nama ekstraktor gallery-dl (BUKAN hostname)
EXTRACTOR_NAMES = {
    "instagram.com": "instagram",
    "x.com": "twitter",
    "twitter.com": "twitter",
    "facebook.com": "facebook",
    "fb.watch": "facebook",
    "pinterest.com": "pinterest",
    "pin.it": "pinterest",
    "threads.net": "threads",
    "reddit.com": "reddit",
    "tumblr.com": "tumblr",
    "flickr.com": "flickr",
    "imgur.com": "imgur",
    "pixiv.net": "pixiv",
    "artstation.com": "artstation",
    "behance.net": "behance",
    "deviantart.com": "deviantart",
    "9gag.com": "9gag",
    "weibo.com": "weibo",
    "vk.com": "vk",
}

SHORT_HOSTS = {
    "pin.it", "youtu.be", "t.co", "fb.watch", "redd.it",
    "vt.tiktok.com", "vm.tiktok.com", "instagr.am",
    "bit.ly", "tinyurl.com", "s.id", "linktr.ee",
}


class DownloadError(RuntimeError):
    pass


@dataclass
class MediaInfo:
    url: str
    title: str
    platform: str
    has_video: bool
    has_image: bool
    has_audio: bool = False
    duration: int | None = None
    thumb: str | None = None


# ─── deteksi engine: tanya library, jangan hardcode ───────────────────
@lru_cache(maxsize=1)
def _yt_classes() -> tuple:
    from yt_dlp.extractor import gen_extractor_classes
    return tuple(gen_extractor_classes())


def yt_dedicated(url: str) -> bool:
    """True jika yt-dlp punya ekstraktor KHUSUS untuk URL ini.

    GenericIE dilewati: ia cocok dengan URL apa pun sehingga hasilnya
    selalu True dan informasinya jadi tak berguna.
    """
    for cls in _yt_classes():
        if cls.ie_key() in ("Generic", "GenericIE"):
            continue
        try:
            if cls._match_valid_url(url):
                return True
        except Exception:
            continue
    return False


def gallery_supported(url: str) -> bool:
    try:
        import gallery_dl.extractor as ge
        return bool(ge.find(url))
    except Exception:
        return False


def engines_for(url: str) -> dict[str, bool]:
    """{'yt-dlp': bool, 'gallery-dl': bool} — True = ada ekstraktor khusus."""
    return {"yt-dlp": yt_dedicated(url), "gallery-dl": gallery_supported(url)}


def is_supported(url: str) -> bool:
    return any(engines_for(url).values())


# ─── util host & cookie ───────────────────────────────────────────────
def _platform_of(url: str) -> str:
    host = re.sub(r"^https?://", "", url, flags=re.I).split("/")[0].lower()
    host = host.split(":")[0].rstrip(".")
    for prefix in ("www.", "m.", "mobile.", "www2."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host


def _cookie_for(url: str) -> Path | None:
    """Hostname -> file cookie. Inilah yang sebelumnya selalu None."""
    host = _platform_of(url)
    for suffix, key in COOKIE_KEYS.items():
        if host == suffix or host.endswith("." + suffix):
            return CONFIG.cookies.get(key)
    return CONFIG.cookies.get(host)


def _extractor_for(url: str) -> str:
    """Hostname -> nama ekstraktor gallery-dl.

    Tidak dipakai runner subprocess (URL diberikan langsung), tapi berguna
    saat menambah opsi per-ekstraktor lewat `-o <extractor>.<key>=<value>`.
    """
    host = _platform_of(url)
    for suffix, name in EXTRACTOR_NAMES.items():
        if host == suffix or host.endswith("." + suffix):
            return name
    return host.split(".")[0]


def _bin(name: str) -> str:
    """Cari console script di venv dulu, lalu PATH."""
    candidate = Path(sys.executable).parent / name
    if candidate.is_file():
        return str(candidate)
    found = shutil.which(name)
    if not found:
        raise DownloadError(
            f"Binary '{name}' tidak ditemukan. Jalankan: pip install {name}"
        )
    return found


def _media_files(outdir: Path) -> list[Path]:
    """Hanya file media jadi — bukan .part, .ytdl, thumbnail, atau direktori."""
    if not outdir.exists():
        return []
    return sorted(
        p for p in outdir.rglob("*")
        if p.is_file() and p.suffix.lower() in MEDIA_EXTS and p.stat().st_size > 0
    )


# ─── subprocess runner ────────────────────────────────────────────────
async def _run(cmd: list[str], timeout: float) -> tuple[int, str]:
    log.debug("exec: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise DownloadError(f"Timeout setelah {timeout:.0f} detik.")
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, (out or b"").decode("utf-8", "replace")


# ─── short link ───────────────────────────────────────────────────────
async def resolve_short_url(url: str, timeout: float = 8.0) -> str:
    """Bentangkan pin.it / youtu.be / t.co / fb.watch ke URL aslinya.

    gallery-dl tidak mengenal pin.it, hanya pinterest.com — tanpa langkah ini
    link share Pinterest selalu gagal.
    """
    if _platform_of(url) not in SHORT_HOSTS:
        return url

    import aiohttp

    try:
        async with aiohttp.ClientSession() as s:
            t = aiohttp.ClientTimeout(total=timeout)
            try:
                async with s.head(url, allow_redirects=True, timeout=t) as r:
                    if r.url and str(r.url) != url:
                        return str(r.url)
            except Exception:
                pass
            # sebagian server menolak HEAD (405/403) → ulangi dengan GET
            async with s.get(url, allow_redirects=True, timeout=t) as r:
                return str(r.url) or url
    except Exception:
        return url


# ─── klasifikasi URL & info ───────────────────────────────────────────
def _url_kind(u: str) -> str | None:
    """Klasifikasi satu URL media. Tahan terhadap query string & CDN tanpa ekstensi.

    Inilah perbaikan inti untuk Pinterest: URL CDN-nya berbentuk
    `https://i.pinimg.com/736x/aa/bb/cc/abcdef.jpg?width=736`, sehingga
    `endswith(".jpg")` gagal dan gambar tidak pernah terdeteksi.
    """
    p = u.split("#", 1)[0].split("?", 1)[0].lower()
    if p.endswith(tuple(VIDEO_EXTS)) or ".m3u8" in p or "/videos/" in p or "/video/" in p:
        return "video"
    if p.endswith(tuple(IMAGE_EXTS)):
        return "image"
    tail = p.rsplit("/", 1)[-1]
    if tail and "." not in tail:
        return "image"          # CDN dinamis: nama file tanpa titik
    return None


def _classify(info: dict) -> tuple[bool, bool, bool]:
    """Tentukan tipe media dari satu info dict yt-dlp."""
    formats = info.get("formats") or []

    def any_codec(key: str) -> bool:
        if info.get(key) not in (None, "none"):
            return True
        return any(f.get(key) not in (None, "none") for f in formats)

    exts = {(f.get("ext") or "").lower() for f in formats}
    exts.add((info.get("ext") or "").lower())

    has_video = any_codec("vcodec") or bool(exts & VIDEO_EXTS)
    has_audio = any_codec("acodec") or bool(exts & AUDIO_EXTS)
    has_image = bool(exts & IMAGE_EXTS)

    # foto tunggal: tidak ada codec video maupun audio
    if not has_image and not has_video and not has_audio:
        has_image = True
    # audio-only (podcast, voice note): perlakukan sebagai video agar tombol
    # "Video" tersedia dan _deliver mengirimnya lewat answer_audio.
    if has_audio and not has_video and not has_image:
        has_video = True

    return has_video, has_image, has_audio


def _title_of(info: dict) -> str:
    for key in ("title", "description", "fulltitle", "id"):
        v = info.get(key)
        if v:
            return str(v)[:200]
    return "media"


# ─── probe: gallery-dl ────────────────────────────────────────────────
async def _probe_gallery(url: str) -> MediaInfo:
    """Enumerasi galeri tanpa mengunduh: `gallery-dl -g` mencetak URL media."""
    cmd = [_bin("gallery-dl"), "-g", "-o", "extractor.range=1-5"]
    cookie = _cookie_for(url)
    if cookie:
        cmd += ["--cookies", str(cookie)]
    cmd.append(url)

    rc, out = await _run(cmd, timeout=30)
    urls = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("http")]
    if not urls:
        raise DownloadError(_friendly(out))

    kinds = {_url_kind(u) for u in urls}
    has_video = "video" in kinds
    # gallery-dl default-nya mengekstrak gambar; jika semua URL tak dikenal,
    # anggap gambar daripada mengembalikan dua-duanya False.
    has_image = "image" in kinds or not has_video

    name = urls[0].split("?", 1)[0].rsplit("/", 1)[-1] or _platform_of(url)
    return MediaInfo(
        url=url,
        title=name[:200],
        platform=_platform_of(url),
        has_video=has_video,
        has_image=has_image,
    )


# ─── probe: yt-dlp ────────────────────────────────────────────────────
async def _probe_ytdlp(url: str) -> MediaInfo:
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "no_color": True,
    }
    cookie = _cookie_for(url)
    if cookie:
        opts["cookiefile"] = str(cookie)

    def _extract() -> dict | None:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await asyncio.to_thread(_extract)
    except Exception as exc:
        raise DownloadError(_friendly(str(exc))) from exc

    if not info:
        raise DownloadError("Tidak ada media yang bisa diekstrak dari URL ini.")

    entries = [e for e in (info.get("entries") or []) if e]
    if entries:
        info = entries[0]

    has_video, has_image, has_audio = _classify(info)
    return MediaInfo(
        url=url,
        title=_title_of(info),
        platform=_platform_of(url),
        has_video=has_video,
        has_image=has_image,
        has_audio=has_audio,
        duration=info.get("duration"),
        thumb=info.get("thumbnail"),
    )


# ─── probe: orkestrasi dua mesin ──────────────────────────────────────
async def probe(url: str) -> MediaInfo:
    platform = _platform_of(url)
    engines = engines_for(url)
    errors: list[str] = []
    info: MediaInfo | None = None

    # 1) host tanpa ekstraktor yt-dlp (Pinterest, Threads foto) -> gallery-dl
    if engines["gallery-dl"] and not engines["yt-dlp"]:
        try:
            info = await _probe_gallery(url)
        except DownloadError as exc:
            errors.append(str(exc))

    # 2) yt-dlp
    if info is None:
        try:
            info = await _probe_ytdlp(url)
        except DownloadError as exc:
            errors.append(str(exc))

    # 3) yt-dlp berhasil tapi tidak menemukan tipe -> coba galeri
    if (
        info is not None
        and not info.has_video
        and not info.has_image
        and engines["gallery-dl"]
    ):
        try:
            info = await _probe_gallery(url)
        except DownloadError:
            pass

    # 4) yt-dlp gagal -> fallback galeri
    if info is None and engines["gallery-dl"]:
        try:
            info = await _probe_gallery(url)
        except DownloadError as exc:
            errors.append(str(exc))

    if info is None:
        raise DownloadError(
            errors[-1] if errors else "Tidak ada media yang bisa diekstrak."
        )

    # 5) jaminan: keyboard tidak boleh hanya berisi Batal
    if not info.has_video and not info.has_image:
        info.has_video = True
        info.has_image = True

    return info


# ─── download: video ──────────────────────────────────────────────────
async def _download_video(url: str, outdir: Path, timeout: float) -> list[Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    mb = CONFIG.max_upload_bytes

    fmt = (
        f"bv*[filesize<{mb}]+ba/"
        f"b[filesize<{mb}]/"
        f"bv*[filesize_approx<{mb}]+ba/"
        f"b[filesize_approx<{mb}]/"
        f"bv*[height<=720]+ba/b[height<=720]/b"
    )

    cmd = [
        _bin("yt-dlp"),
        "-f", fmt,
        "--merge-output-format", "mp4",
        "-o", str(outdir / "%(title).80s-%(id)s.%(ext)s"),
        "--restrict-filenames",
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--retries", "3",
        "--fragment-retries", "3",
        "--socket-timeout", "30",
        "--concurrent-fragments", "4",
    ]
    cookie = _cookie_for(url)
    if cookie:
        cmd += ["--cookies", str(cookie)]
    cmd.append(url)

    rc, out = await _run(cmd, timeout)
    files = [
        p for p in _media_files(outdir)
        if p.suffix.lower() in VIDEO_EXTS | AUDIO_EXTS
    ]
    if rc != 0 and not files:
        raise DownloadError(_friendly(out))
    return files


# ─── download: gambar ─────────────────────────────────────────────────
async def _download_images(url: str, outdir: Path, timeout: float) -> list[Path]:
    outdir.mkdir(parents=True, exist_ok=True)

    cmd = [
        _bin("gallery-dl"),
        "-d", str(outdir),
        "-o", "extractor.range=1-10",
        "--no-mtime",
    ]
    cookie = _cookie_for(url)
    if cookie:
        cmd += ["--cookies", str(cookie)]
    cmd.append(url)

    rc, out = await _run(cmd, timeout)
    files = _media_files(outdir)[:10]
    if rc != 0 and not files:
        raise DownloadError(_friendly(out))
    return files


# ─── download: orkestrasi ─────────────────────────────────────────────
async def download(
    url: str,
    mode: str,
    workdir: Path,
    timeout: float = 180.0,
) -> list[Path]:
    """mode: 'video' | 'image' | 'both'"""
    if mode not in ("video", "image", "both"):
        raise ValueError(f"mode tidak dikenal: {mode}")

    if mode == "video":
        return await _download_video(url, workdir / "video", timeout)

    if mode == "image":
        return await _download_images(url, workdir / "image", timeout)

    # both: subdirektori terpisah, deadline bersama supaya tidak 2x timeout
    deadline = time.monotonic() + timeout
    results: list[Path] = []
    errors: list[str] = []

    for name, fn, sub in (
        ("gambar", _download_images, "image"),
        ("video", _download_video, "video"),
    ):
        remaining = max(deadline - time.monotonic(), 5.0)
        try:
            results += await fn(url, workdir / sub, remaining)
        except DownloadError as exc:
            errors.append(f"{name}: {exc}")

    if not results:
        raise DownloadError("; ".join(errors) or "Tidak ada file yang berhasil diunduh.")
    return results


# ─── pesan & util ─────────────────────────────────────────────────────
def extract_url(text: str) -> str | None:
    m = URL_RE.search(text or "")
    if not m:
        return None
    url = m.group(0)
    while url and url[-1] in ".,;:!?'\"":
        url = url[:-1]
    # buang ')' hanya jika tidak berpasangan — jaga URL seperti /Foo_(bar)
    if url.endswith(")") and url.count("(") < url.count(")"):
        url = url[:-1]
    return url or None


def _friendly(text: str) -> str:
    low = text.lower()
    if "sign in" in low or "login" in low or "cookies" in low:
        return "Konten ini butuh login. Tambahkan cookies platform ke folder cookies/."
    if "private" in low:
        return "Konten privat — tidak bisa diakses tanpa akun yang punya izin."
    if "unsupported url" in low or "no suitable extractor" in low:
        return "Platform ini belum punya ekstraktor. Coba link media langsung."
    # "not found", "notfound", "could not be found", "404"
    if "notfound" in low or "not found" in low or "not be found" in low or re.search(r"\b404\b", text):
        return "Media tidak ditemukan. Link mungkin salah, sudah dihapus, atau pin-nya privat."
    if "timed out" in low or "timeout" in low:
        return "Server sumber lambat merespons. Coba lagi."
    if "ffmpeg" in low:
        return "ffmpeg tidak ditemukan. Pasang: sudo pacman -S ffmpeg"
    return text.strip()[:300] or "Gagal memproses URL."


def cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)