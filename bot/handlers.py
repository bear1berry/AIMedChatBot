# bot/handlers.py
from __future__ import annotations

import logging
from typing import Dict

from aiogram import Router, F, Bot
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

from .config import settings
from .ai_client import ask_ai
from .modes import MODES, detect_mode
from .memory import register_user, get_stats, save_message
from .vision import analyze_image

logger = logging.getLogger(__name__)

router = Router(name="main")

# Память по режимам (в рамках процесса)
_user_modes: Dict[int, str] = {}


def _get_user_mode(user_id: int) -> str:
    return _user_modes.get(user_id, "default")


def _set_user_mode(user_id: int, mode: str) -> None:
    if mode not in MODES:
        mode = "default"
    _user_modes[user_id] = mode


def _is_allowed(username: str | None) -> bool:
    if not settings.allowed_users:
        return True
    if not username:
        return False
    return username.lstrip("@") in settings.allowed_users


def _modes_keyboard(current_mode: str) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for code, cfg in MODES.items():
        mark = "✅" if code == current_mode else "⚪️"
        kb.button(
            text=f"{mark} {cfg['short_name']}",
            callback_data=f"set_mode:{code}",
        )
    kb.adjust(2)
    return kb


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user = message.from_user
    if not user:
        return

    if not _is_allowed(user.username):
        await message.answer(
            "🚫 Доступ к этому боту ограничен.\n"
            "Если вы считаете, что это ошибка — напишите владельцу бота."
        )
        return

    register_user(user.id, user.username)
    _set_user_mode(user.id, "default")

    text = (
        "👋 Привет! Я медицинский AI-бот.\n\n"
        "Отправь мне свой вопрос — разберёмся по-взрослому.\n\n"
        "Ниже можешь выбрать профиль работы:"
    )

    await message.answer(
        text,
        reply_markup=_modes_keyboard("default").as_markup(),
    )


@router.callback_query(F.data.startswith("set_mode:"))
async def cb_set_mode(callback: CallbackQuery) -> None:
    user = callback.from_user
    if not user:
        return

    if not _is_allowed(user.username):
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    _, mode = callback.data.split(":", 1)
    if mode not in MODES:
        await callback.answer("Неизвестный режим", show_alert=True)
        return

    current_mode = _get_user_mode(user.id)
    if mode == current_mode:
        # Режим уже выбран — просто покажем подсказку
        await callback.answer("Этот режим уже выбран ✅")
        return

    _set_user_mode(user.id, mode)
    cfg = MODES[mode]

    # Обновляем клавиатуру, но игнорируем "message is not modified"
    try:
        if callback.message:
            await callback.message.edit_reply_markup(
                reply_markup=_modes_keyboard(mode).as_markup()
            )
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            logger.debug("Ignored Telegram 'message is not modified' for mode=%s", mode)
        else:
            raise

    await callback.answer(f"Режим: {cfg['title']}")


@router.message(Command("mode"))
async def cmd_mode(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    if not _is_allowed(user.username):
        await message.answer("🚫 Доступ к этому боту ограничен.")
        return

    current = _get_user_mode(user.id)
    await message.answer(
        "Выбери режим работы бота:",
        reply_markup=_modes_keyboard(current).as_markup(),
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    user = message.from_user
    if not user:
        return

    if settings.admin_username and user.username and user.username.lstrip("@") == settings.admin_username:
        s = get_stats()
        await message.answer(
            f"📊 Статистика:\n"
            f"Пользователей: <b>{s['users']}</b>\n"
            f"Сообщений: <b>{s['messages']}</b>"
        )
    else:
        await message.answer("Команда доступна только администратору.")


@router.message(F.photo)
async def photo_handler(message: Message, bot: Bot) -> None:
    user = message.from_user
    if not user:
        return
    if not _is_allowed(user.username):
        await message.answer("🚫 Доступ к этому боту ограничен.")
        return

    photo = message.photo[-1]

    # Скачиваем bytes
    file_obj = await bot.download(photo)
    image_bytes = file_obj.read()

    await message.answer("🖼 Получил изображение, анализирую...")

    reply = await analyze_image(image_bytes, user_id=user.id)
    save_message(user.id, "user", "[изображение]", "vision")
    save_message(user.id, "assistant", reply, "vision")

    await message.answer(reply)


@router.message(F.text)
async def text_handler(message: Message) -> None:
    user = message.from_user
    if not user:
        return

    if not _is_allowed(user.username):
        await message.answer("🚫 Доступ к этому боту ограничен.")
        return

    text = message.text or ""
    current_mode = _get_user_mode(user.id)

    # Автоопределение режима
    detected_mode = detect_mode(text, current_mode=current_mode)
    if detected_mode != current_mode:
        _set_user_mode(user.id, detected_mode)
        mode_changed_note = (
            f"🔁 Автоматически переключился в режим: "
            f"<b>{MODES[detected_mode]['title']}</b>\n\n"
        )
    else:
        mode_changed_note = ""

    await message.chat.do("typing")

    reply = await ask_ai(
        user_id=user.id,
        mode=detected_mode,
        user_message=text,
    )

    final_text = mode_changed_note + reply

    await message.answer(
        final_text,
        reply_markup=_modes_keyboard(detected_mode).as_markup(),
    )
