"""Entrypoint bot. Satu router, alur tiga langkah:

link masuk -> probe -> keyboard pilih tipe -> unduh -> kirim -> cleanup
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import tempfile
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, FSInputFile, Message

import downloader as dl
from config import CONFIG
from keyboards import type_keyboard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
log = logging.getLogger("media-bot")

router = Router()
_sem = asyncio.Semaphore(CONFIG.max_concurrent)

# token -> MediaInfo, dibuang setelah dipakai atau kedaluwarsa
_pending: dict[str, tuple[dl.MediaInfo, float]] = {}
PENDING_TTL = 600  # 10 menit

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov"}


# ─── perintah ────────────────────────────────────────────────────────
@router.message(CommandStart())
async def cmd_start(m: Message) -> None:
    await m.answer(
        "<b>Media Downloader</b>\n\n"
        "Kirim satu link dari platform mana pun — YouTube, TikTok, Instagram, "
        "X/Twitter, Facebook, Reddit, Pinterest, Threads, Vimeo, dan 1800+ situs lain.\n\n"
        "Setelah link terdeteksi, saya tanya dulu mau ambil video, gambar, atau keduanya.\n\n"
        "<i>Batasan Telegram Bot API: 50 MB per file.</i>",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("help"))
async def cmd_help(m: Message) -> None:
    await cmd_start(m)


@router.message(Command("cookie"))
async def cmd_cookie(m: Message) -> None:
    active = ", ".join(CONFIG.cookies) or "belum ada"
    await m.answer(
        f"Cookie aktif: <b>{active}</b>\n\n"
        "Untuk konten yang butuh login, letakkan file di folder <code>cookies/</code>:\n"
        "<code>instagram.txt</code>, <code>x.txt</code>, <code>facebook.txt</code>\n\n"
        "Ekspor dengan ekstensi <i>Get cookies.txt LOCALLY</i>, lalu restart bot.",
        parse_mode=ParseMode.HTML,
    )


# ─── link masuk ──────────────────────────────────────────────────────
@router.message(F.text)
async def on_text(m: Message) -> None:
    url = dl.extract_url(m.text or "")
    if not url:
        return await m.reply(
            "Tidak ada URL di pesan itu. Kirim satu link langsung."
        )

    if not dl.is_supported(url):
        return await m.reply(
            "Domain itu belum ada di daftar cepat. Saya coba tetap — kalau gagal, "
            "kemungkinan butuh login."
        )

    status = await m.reply("🔍 Membaca metadata…")

    try:
        info = await dl.probe(url)
    except dl.DownloadError as exc:
        await status.edit_text(f"❌ {exc}")
        return
    except Exception as exc:
        log.exception("probe gagal: %s", url)
        await status.edit_text(f"❌ Gagal membaca media: {str(exc)[:200]}")
        return

    token = secrets.token_urlsafe(8)
    _pending[token] = (info, time.monotonic())
    _sweep_pending()

    duration = f" · {info.duration}s" if info.duration else ""
    await status.edit_text(
        f"<b>{info.title}</b>\n"
        f"<code>{info.platform}</code>{duration}\n\n"
        "Pilih yang mau diunduh:",
        reply_markup=type_keyboard(info, token),
        parse_mode=ParseMode.HTML,
    )


# ─── callback tombol ─────────────────────────────────────────────────
@router.callback_query(F.data.startswith("dl:"))
async def on_choice(cq: CallbackQuery) -> None:
    _, mode, token = (cq.data or "").split(":", 2)
    entry = _pending.pop(token, None)

    if entry is None:
        return await cq.answer("Sesi kedaluwarsa. Kirim link lagi.", show_alert=True)

    info, _ = entry
    await cq.answer()

    if mode == "cancel":
        await cq.message.edit_text("Dibatalkan.")
        return

    if mode == "video" and not info.has_video:
        return await cq.message.edit_text("Media ini tidak punya video.")
    if mode == "image" and not info.has_image:
        return await cq.message.edit_text("Media ini tidak punya gambar.")

    label = {"video": "video", "image": "gambar", "both": "semua media"}[mode]
    await cq.message.edit_text(f"⬇️ Mengunduh {label}…")

    async with _sem:
        with tempfile.TemporaryDirectory(dir=CONFIG.work_dir, prefix="job-") as tmp:
            workdir = Path(tmp)
            try:
                await cq.bot.send_chat_action(cq.message.chat.id, ChatAction.UPLOAD_VIDEO)
                files = await asyncio.wait_for(
                    dl.download(info.url, mode, workdir), timeout=180
                )
            except asyncio.TimeoutError:
                await cq.message.edit_text("⏱ Timeout setelah 180 detik. Coba link lain.")
                return
            except Exception as exc:
                log.exception("download gagal: %s", info.url)
                msg = exc.args[0] if isinstance(exc, dl.DownloadError) else str(exc)[:200]
                await cq.message.edit_text(f"❌ {msg}")
                return

            if not files:
                await cq.message.edit_text("Tidak ada file yang berhasil diunduh.")
                return

            sent = await _deliver(cq.message, files, info)
            await cq.message.edit_text(
                f"✅ Selesai — {sent} file terkirim.\n"
                f"<a href=\"{info.url}\">sumber</a>",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )


# ─── pengiriman file ─────────────────────────────────────────────────
async def _deliver(msg: Message, files: list[Path], info: dl.MediaInfo) -> int:
    """Kirim sesuai tipe. Batas Telegram ditegakkan di sini, bukan di scraper."""
    sent = 0
    limit = CONFIG.max_upload_bytes

    images = [f for f in files if f.suffix.lower() in IMAGE_EXT and f.stat().st_size <= limit]
    videos = [f for f in files if f.suffix.lower() in VIDEO_EXT and f.stat().st_size <= limit]
    oversize = [f for f in files if f.stat().st_size > limit]

    # foto: album kalau >1
    for i in range(0, len(images), 10):
        chunk = images[i:i + 10]
        if not chunk:
            continue
        try:
            if len(chunk) == 1:
                await msg.answer_photo(FSInputFile(chunk[0]), caption=info.title[:900])
            else:
                from aiogram.types import InputMediaPhoto
                await msg.answer_media_group([
                    InputMediaPhoto(media=FSInputFile(p)) for p in chunk
                ])
            sent += len(chunk)
        except Exception:
            log.exception("kirim foto gagal")
            for p in chunk:
                try:
                    await msg.answer_document(FSInputFile(p), caption=p.name)
                    sent += 1
                except Exception:
                    log.exception("fallback dokumen gagal: %s", p)

    for v in videos:
        try:
            await msg.answer_video(
                FSInputFile(v),
                caption=info.title[:900],
                supports_streaming=True,
                duration=info.duration,
            )
            sent += 1
        except Exception:
            log.exception("kirim video gagal, coba dokumen: %s", v)
            try:
                await msg.answer_document(FSInputFile(v), caption=v.name)
                sent += 1
            except Exception:
                pass

    if oversize:
        names = ", ".join(f.name for f in oversize[:3])
        await msg.answer(
            f"⚠️ {len(oversize)} file melebihi {CONFIG.max_upload_mb} MB "
            f"dan dilewati: <code>{names}</code>\n"
            "Naikkan MAX_UPLOAD_MB hanya jika bot Anda memakai server Bot API lokal.",
            parse_mode=ParseMode.HTML,
        )

    return sent


# ─── housekeeping ────────────────────────────────────────────────────
def _sweep_pending() -> None:
    now = time.monotonic()
    for k in [k for k, (_, t) in _pending.items() if now - t > PENDING_TTL]:
        _pending.pop(k, None)


# ─── entrypoint ──────────────────────────────────────────────────────
async def main() -> None:
    bot = Bot(
        token=CONFIG.token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    me = await bot.get_me()
    log.info("bot aktif: @%s", me.username)
    log.info("cookie terdeteksi: %s", list(CONFIG.cookies) or "tidak ada")

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("berhenti")