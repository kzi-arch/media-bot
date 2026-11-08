"""Konfigurasi terpusat. Semua nilai bisa dioverride lewat environment."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Config:
    token: str
    max_upload_mb: int = 50
    work_dir: Path = Path("/tmp/media-bot")
    max_concurrent: int = 3
    cookies: dict[str, Path] = field(default_factory=dict)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


def _cookie(env_key: str, default: str) -> Path | None:
    raw = os.getenv(env_key, default)
    p = ROOT / raw
    return p if p.is_file() else None


def load_config() -> Config:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN belum diisi di .env")

    work = Path(os.getenv("WORK_DIR", "/tmp/media-bot"))
    work.mkdir(parents=True, exist_ok=True)

    cookies = {}
    for platform, env_key, default in (
        ("instagram", "COOKIE_INSTAGRAM", "cookies/instagram.txt"),
        ("x",         "COOKIE_X",         "cookies/x.txt"),
        ("facebook",  "COOKIE_FACEBOOK",  "cookies/facebook.txt"),
        ("pinterest", "COOKIE_PINTEREST", "cookies/pinterest.txt"),   # ← baru
        ("threads",   "COOKIE_THREADS",   "cookies/threads.txt"),     # ← baru
    ):
        path = _cookie(env_key, default)
        if path:
            # pastikan hanya owner yang bisa baca
            path.chmod(0o600)
            cookies[platform] = path

    return Config(
        token=token,
        max_upload_mb=int(os.getenv("MAX_UPLOAD_MB", "50")),
        work_dir=work,
        max_concurrent=int(os.getenv("MAX_CONCURRENT", "3")),
        cookies=cookies,
    )


CONFIG = load_config()