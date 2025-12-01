from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .modes import CHAT_MODES, DEFAULT_MODE_KEY


def build_modes_keyboard(current_mode: str | None = None) -> InlineKeyboardMarkup:
    if not current_mode or current_mode not in CHAT_MODES:
        current_mode = DEFAULT_MODE_KEY

    kb = InlineKeyboardBuilder()

    # Фиксированный порядок режимов
    order = [
        "ai_medicine_assistant",
        "chatgpt_style",
        "personal_companion",
        "content_maker",
        "pure_chatgpt",
    ]

    for key in order:
        mode = CHAT_MODES.get(key)
        if mode is None:
            continue
        mark = "✅" if key == current_mode else "⚪️"
        kb.button(text=f"{mark} {mode.title}", callback_data=f"mode:{key}")

    kb.adjust(2)
    return kb.as_markup()
