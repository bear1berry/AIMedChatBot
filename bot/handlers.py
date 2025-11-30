# bot/handlers.py

from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.filters import CommandStart, Command

import logging

from .config import settings
from .ai_client import ask_ai
from .vision import analyze_image
from .modes import (
    MODES,
    MODES_ORDER,
    DEFAULT_MODE,
    build_modes_keyboard,
    auto_detect_mode,
    get_modes_human_readable,
)
from .memory import register_user, active_users, log_request


router = Router()

# Храним активный режим пользователя
user_modes = {}

# ---------------------------------------------------------------------------
# 🔐 ACCESS CONTROL
# ---------------------------------------------------------------------------

def check_access(username: str) -> bool:
    return username.lower() in [u.lower() for u in settings.allowed_users]


def is_admin(username: str) -> bool:
    return username.lower() == settings.admin_user.lower()


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def start(message: Message):
    username = (message.from_user.username or "").lower()

    if not check_access(username):
        return await message.answer("🚫 У вас нет доступа к этому боту.")

    # регистрируем пользователя
    register_user(message.from_user.id, username)

    user_modes[message.from_user.id] = DEFAULT_MODE

    text = (
        "<b>Привет!</b> Добро пожаловать в AI Medicine Bot.\n\n"
        "<b>Доступные режимы:</b>\n"
        f"{get_modes_human_readable()}\n\n"
        "Выбери режим 👇 или просто напиши вопрос — бот сам подберёт."
    )

    await message.answer(
        text,
        reply_markup=build_modes_keyboard()
    )


# ---------------------------------------------------------------------------
# /users — только администратор
# ---------------------------------------------------------------------------

@router.message(Command("users"))
async def cmd_users(message: Message):
    username = (message.from_user.username or "").lower()

    if not is_admin(username):
        return await message.answer("🚫 Только администратор может выполнять эту команду.")

    if not active_users:
        return await message.answer("Пока ещё никто не обращался.")

    text = "📋 <b>Подключённые пользователи:</b>\n\n"

    for uid, uname in active_users:
        text += f"• @{uname} — ID: <code>{uid}</code>\n"

    await message.answer(text)


# ---------------------------------------------------------------------------
# 📌 Callback-кнопки выбора режима
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("mode:"))
async def mode_selected(callback: CallbackQuery):
    mode_key = callback.data.split(":", 1)[1]

    if mode_key not in MODES:
        return await callback.answer("Неизвестный режим", show_alert=True)

    user_modes[callback.from_user.id] = mode_key
    title = MODES[mode_key]["title"]
    emoji = MODES[mode_key]["emoji"]

    await callback.answer(f"Режим переключён: {emoji} {title}")
    await callback.message.answer(
        f"🔄 Режим изменён: <b>{emoji} {title}</b>\n\nМожешь продолжать, я готов!"
    )


# ---------------------------------------------------------------------------
# 🖼 ОБРАБОТКА ИЗОБРАЖЕНИЙ
# ---------------------------------------------------------------------------

@router.message(F.photo)
async def photo_handler(message: Message):
    username = (message.from_user.username or "").lower()

    if not check_access(username):
        return await message.answer("🚫 Нет доступа.")

    register_user(message.from_user.id, username)

    file_id = message.photo[-1].file_id
    file_info = await message.bot.get_file(file_id)
    file_bytes = await message.bot.download_file(file_info.file_path)

    await message.answer("🔍 Анализирую изображение…")

    result = await analyze_image(file_bytes.read())

    # Логируем
    log_request(
        user_id=message.from_user.id,
        username=username,
        mode="vision",
        query="IMAGE_UPLOADED",
        reply=result,
    )

    await message.answer(result)


# ---------------------------------------------------------------------------
# ✉ ТЕКСТОВЫЕ ЗАПРОСЫ
# ---------------------------------------------------------------------------

@router.message(F.text)
async def text_handler(message: Message):
    username = (message.from_user.username or "").lower()

    if not check_access(username):
        return await message.answer("🚫 У вас нет доступа.")

    user_id = message.from_user.id

    # Регистрируем в БД
    register_user(user_id, username)

    # Получаем текущий режим
    current_mode = user_modes.get(user_id, DEFAULT_MODE)

    # Авто-распознавание режима по тексту
    auto_mode = auto_detect_mode(message.text)

    if auto_mode != DEFAULT_MODE and auto_mode != current_mode:
        current_mode = auto_mode
        user_modes[user_id] = current_mode
        await message.answer(
            f"🔄 Автоматически выбран режим: <b>{MODES[current_mode]['emoji']} {MODES[current_mode]['title']}</b>"
        )

    await message.answer("🧠 Думаю над ответом…")

    reply = await ask_ai(
        user_id=user_id,
        mode=current_mode,
        user_message=message.text
    )

    # Логирование
    log_request(
        user_id=user_id,
        username=username,
        mode=current_mode,
        query=message.text,
        reply=reply,
    )

    await message.answer(reply, reply_markup=build_modes_keyboard())
