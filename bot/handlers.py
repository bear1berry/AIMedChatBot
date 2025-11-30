import logging
from typing import Dict

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from .config import settings
from .ai_client import ask_ai
from .vision import analyze_image, transcribe_audio, generate_image
from .modes import MODES, detect_mode
from .memory import register_user, log_message, get_users

router = Router()
logger = logging.getLogger(__name__)

# Текущий режим пользователя в памяти
user_modes: Dict[int, str] = {}


# === Доступ / права ===================================================


def check_access(username: str | None) -> bool:
    if not username:
        return False
    return username.lower() in [u.lower() for u in settings.allowed_users]


def is_admin(username: str | None) -> bool:
    if not username:
        return False
    return username.lower() == settings.admin_user.lower()


# === Inline-клавиатура режимов ========================================


def mode_keyboard(current_mode: str | None = None) -> InlineKeyboardMarkup:
    buttons = []
    row = []

    def add_btn(mode_id: str):
        mode = MODES[mode_id]
        prefix = "✅ " if current_mode == mode_id else ""
        row.append(
            InlineKeyboardButton(
                text=f"{prefix}{mode.emoji} {mode.title}", callback_data=f"mode:{mode_id}"
            )
        )
        if len(row) == 2:
            buttons.append(row.copy())
            row.clear()

    order = ["default", "simple", "medical", "symptoms",
             "pediatrics", "dermatology", "ophthalmology",
             "gynecology", "cardiology"]

    for m_id in order:
        if m_id in MODES:
            add_btn(m_id)

    if row:
        buttons.append(row)

    return InlineKeyboardMarkup(inline_keyboard=buttons)


# === /start ===========================================================


@router.message(CommandStart())
async def start(message: Message):
    username = message.from_user.username

    if not check_access(username):
        await message.answer("🚫 У вас нет доступа к этому боту.")
        return

    register_user(message.from_user.id, username)
    user_modes[message.from_user.id] = "default"

    await message.answer(
        "Привет! Я AI Medicine Bot.\n\n"
        "Я могу помогать с медицинскими вопросами в образовательном формате.\n"
        "Режим можно переключать кнопками ниже.\n\n"
        "Важно: я не ставлю диагнозы и не назначаю лечение. "
        "При любых сомнениях обязательно обращайтесь к врачу.",
        reply_markup=mode_keyboard("default"),
    )


# === Команда /modes – показать клавиатуру =============================


@router.message(Command("modes"))
async def cmd_modes(message: Message):
    username = message.from_user.username
    if not check_access(username):
        return await message.answer("🚫 Нет доступа.")

    mode = user_modes.get(message.from_user.id, "default")
    await message.answer("Выбери режим работы:", reply_markup=mode_keyboard(mode))


# === Команда /users – только для админа ===============================


@router.message(Command("users"))
async def cmd_users(message: Message):
    username = message.from_user.username

    if not is_admin(username):
        return await message.answer("🚫 Только администратор может выполнять эту команду.")

    rows = get_users()

    if not rows:
        return await message.answer("Пока ещё никто не обращался к боту.")

    text_lines = ["📋 Подключённые пользователи:\n"]
    for row in rows:
        uid = row["user_id"]
        uname = row["username"] or "—"
        last_seen = row["last_seen"]
        text_lines.append(f"• @{uname} (id: {uid}, last_seen: {last_seen})")

    await message.answer("\n".join(text_lines))


# === Переключение режима по callback-кнопкам ==========================


@router.callback_query(F.data.startswith("mode:"))
async def cb_mode(callback: CallbackQuery):
    username = callback.from_user.username
    if not check_access(username):
        await callback.answer("Нет доступа", show_alert=True)
        return

    mode_id = callback.data.split(":", 1)[1]
    if mode_id not in MODES:
        await callback.answer("Неизвестный режим", show_alert=True)
        return

    user_modes[callback.from_user.id] = mode_id
    mode = MODES[mode_id]
    await callback.answer(f"Режим: {mode.title}", show_alert=False)
    await callback.message.edit_reply_markup(reply_markup=mode_keyboard(mode_id))


# === Текстовые команды переключения (на всякий случай) ===============


@router.message(Command("mode_default"))
async def m_default(msg: Message):
    user_modes[msg.from_user.id] = "default"
    await msg.answer("Режим: Обычный 💬")


@router.message(Command("mode_simple"))
async def m_simple(msg: Message):
    user_modes[msg.from_user.id] = "simple"
    await msg.answer("Режим: Простыми словами 🧠")


@router.message(Command("mode_medical"))
async def m_medical(msg: Message):
    user_modes[msg.from_user.id] = "medical"
    await msg.answer("Режим: Медицинский справочник 📚")


@router.message(Command("mode_symptoms"))
async def m_symptoms(msg: Message):
    user_modes[msg.from_user.id] = "symptoms"
    await msg.answer("Режим: Анализ симптомов 🔍")


# === Генерация изображений /image ====================================


@router.message(Command("image"))
async def cmd_image(message: Message):
    username = message.from_user.username
    if not check_access(username):
        return await message.answer("🚫 Нет доступа.")

    prompt = message.text.partition(" ")[2].strip()
    if not prompt:
        return await message.answer("Напиши запрос после команды, например:\n/image минималистичный медицинский постер про вакцинацию")

    await message.answer("🎨 Генерирую изображение…")

    url = await generate_image(prompt)
    if not url:
        return await message.answer(
            "Генерация изображений недоступна (нет API-ключа OpenAI)."
        )

    await message.answer_photo(url, caption=f"Запрос: {prompt}")


# === Анализ изображений ==============================================


@router.message(F.photo)
async def photo_handler(message: Message):
    username = message.from_user.username
    if not check_access(username):
        return await message.answer("🚫 Нет доступа.")

    register_user(message.from_user.id, username)

    file_id = message.photo[-1].file_id
    file = await message.bot.get_file(file_id)
    file_bytes = await message.bot.download_file(file.file_path)
    data = file_bytes.read()

    await message.answer("🔍 Анализирую изображение…")

    result = await analyze_image(data)
    await message.answer(result)


# === Голосовые сообщения =============================================


@router.message(F.voice)
async def voice_handler(message: Message):
    username = message.from_user.username
    if not check_access(username):
        return await message.answer("🚫 Нет доступа.")

    register_user(message.from_user.id, username)

    file = await message.bot.get_file(message.voice.file_id)
    file_bytes = await message.bot.download_file(file.file_path)
    audio_data = file_bytes.read()

    await message.answer("🗣 Распознаю голосовое сообщение…")

    try:
        text = await transcribe_audio(audio_data, filename="voice.ogg")
    except Exception as e:
        logger.exception("Whisper error: %s", e)
        return await message.answer("Не удалось распознать голосовое сообщение.")

    if not text:
        return await message.answer("Текст не распознан.")

    await message.answer(f"Я распознал текст:\n\n{text}\n\nТеперь думаю над ответом…")

    # используем тот же поток, что и для обычного текста
    mode = user_modes.get(message.from_user.id, "default")
    log_message(message.from_user.id, "user", text, mode)

    reply = await ask_ai(
        user_id=message.from_user.id,
        mode=mode,
        user_message=text,
    )
    await message.answer(reply)


# === Основная логика текста ==========================================


@router.message(F.text)
async def text_handler(message: Message):
    username = message.from_user.username
    if not check_access(username):
        return await message.answer("🚫 Нет доступа.")

    register_user(message.from_user.id, username)

    explicit_mode = user_modes.get(message.from_user.id, "default")
    auto_mode = detect_mode(message.text)

    # Если авто-режим более специфичный, используем его
    if auto_mode != "default" and auto_mode != explicit_mode:
        mode = auto_mode
    else:
        mode = explicit_mode

    user_modes[message.from_user.id] = mode  # запоминаем последний использованный режим

    log_message(message.from_user.id, "user", message.text, mode)

    await message.answer("Думаю над ответом… 🧠")

    reply = await ask_ai(
        user_id=message.from_user.id,
        mode=mode,
        user_message=message.text,
    )

    await message.answer(reply)
