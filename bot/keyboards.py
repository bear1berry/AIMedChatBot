from __future__ import annotations

from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardMarkup

from .modes import MODES


def modes_keyboard(current_mode: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    order = ["default", "symptoms", "pediatrics", "ophthalmology", "dermatology", "infections", "vision"]
    for code in order:
        cfg = MODES[code]
        mark = "✅" if code == current_mode else "⚪️"
        kb.button(
            text=f"{mark} {cfg['short_name']}",
            callback_data=f"set_mode:{code}",
        )
    kb.adjust(2)
    return kb.as_markup()


def main_menu_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🩺 Симптом-чекер", callback_data="menu:symptoms")
    kb.button(text="💬 Задать вопрос", callback_data="menu:ask")
    kb.button(text="🧾 Мои случаи", callback_data="menu:cases")
    kb.button(text="⚙️ Профиль", callback_data="menu:profile")
    kb.adjust(2)
    return kb.as_markup()


def answer_with_modes_keyboard(current_mode: str) -> InlineKeyboardMarkup:
    """
    Клавиатура под каждым ответом:
    первая строка — действия над ответом,
    дальше — переключатель режимов.
    """
    kb = InlineKeyboardBuilder()
    # actions
    kb.button(text="📝 Конспект", callback_data="act:summary")
    kb.button(text="❓ Уточнить", callback_data="act:followup")
    kb.button(text="📩 Для пациента", callback_data="act:for_patient")
    kb.adjust(3)

    # modes
    order = ["default", "symptoms", "pediatrics", "ophthalmology", "dermatology", "infections"]
    for code in order:
        cfg = MODES[code]
        mark = "✅" if code == current_mode else "•"
        kb.button(
            text=f"{mark} {cfg['short_name']}",
            callback_data=f"set_mode:{code}",
        )
    kb.adjust(3)
    return kb.as_markup()
