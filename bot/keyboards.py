# bot/keyboards.py

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from .modes import MODES

def mode_keyboard():
    buttons = []
    row = []

    for key, data in MODES.items():
        row.append(
            InlineKeyboardButton(
                text=f"{data['emoji']} {data['name']}",
                callback_data=f"mode_{key}"
            )
        )

        # по 2 кнопки в ряд
        if len(row) == 2:
            buttons.append(row)
            row = []

    if row:
        buttons.append(row)

    return InlineKeyboardMarkup(inline_keyboard=buttons)
