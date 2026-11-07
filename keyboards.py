"""Inline keyboard: pemilihan tipe media setelah link dikirim."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from downloader import MediaInfo


def type_keyboard(info: MediaInfo, token: str) -> InlineKeyboardMarkup:
    """Tiga tombol; yang tidak mungkin dihilangkan."""
    rows: list[list[InlineKeyboardButton]] = []

    buttons: list[InlineKeyboardButton] = []
    if info.has_video:
        buttons.append(
            InlineKeyboardButton(text="🎬 Video", callback_data=f"dl:video:{token}")
        )
    if info.has_image:
        buttons.append(
            InlineKeyboardButton(text="🖼 Gambar", callback_data=f"dl:image:{token}")
        )
    if info.has_video and info.has_image:
        buttons.append(
            InlineKeyboardButton(text="📦 Keduanya", callback_data=f"dl:both:{token}")
        )

    if buttons:
        rows.append(buttons)

    rows.append([
        InlineKeyboardButton(text="✖ Batal", callback_data=f"dl:cancel:{token}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)