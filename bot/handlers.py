from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram.utils.chat_action import ChatActionSender

from .ai_client import ask_ai, get_state, reset_state, set_mode
from .config import settings
from .keyboards import build_modes_keyboard
from .limits import check_rate_limit
from .modes import DEFAULT_MODE_KEY, CHAT_MODES, get_mode_label

router = Router()
logger = logging.getLogger(__name__)

MAX_TELEGRAM_MESSAGE_LEN = 4096


# --- Вспомогательные функции ---


def _split_text(text: str) -> list[str]:
    """
    Делим длинный текст на части < 4096 символов, стараясь резать по абзацам.
    """
    if len(text) <= MAX_TELEGRAM_MESSAGE_LEN:
        return [text]

    parts: list[str] = []
    rest = text.strip()

    while len(rest) > MAX_TELEGRAM_MESSAGE_LEN:
        cut = rest.rfind("\n\n", 0, MAX_TELEGRAM_MESSAGE_LEN)
        if cut == -1:
            cut = rest.rfind("\n", 0, MAX_TELEGRAM_MESSAGE_LEN)
        if cut == -1:
            cut = rest.rfind(" ", 0, MAX_TELEGRAM_MESSAGE_LEN)
        if cut == -1:
            cut = MAX_TELEGRAM_MESSAGE_LEN

        chunk = rest[:cut].strip()
        if chunk:
            parts.append(chunk)
        rest = rest[cut:].lstrip()

    if rest:
        parts.append(rest)

    return parts


# --- Команды ---


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user = message.from_user
    assert user is not None

    state = get_state(user.id)
    current_mode_key = state.mode_key or DEFAULT_MODE_KEY
    current_mode_label = get_mode_label(current_mode_key)

    kb = build_modes_keyboard(current_mode_key)

    text = (
        f"Привет, {user.first_name or 'друг'}! 👋\n\n"
        "Я твой ИИ-ассистент для проекта *AI Medicine*.\n"
        "Помогу с медицинскими вопросами, идеями для постов и просто поболтать.\n\n"
        f"Текущий режим: *{current_mode_label}*\n\n"
        "✏️ Просто напиши свой вопрос ниже — я отвечу.\n"
        "Чтобы сменить стиль работы, используй кнопки ниже."
    )

    await message.answer(text, reply_markup=kb)


@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    user = message.from_user
    assert user is not None
    reset_state(user.id)
    await message.answer("🧹 История диалога очищена. Начнём заново!")


@router.message(Command("mode"))
async def cmd_mode(message: Message) -> None:
    user = message.from_user
    assert user is not None

    state = get_state(user.id)
    current_mode_key = state.mode_key or DEFAULT_MODE_KEY

    kb = build_modes_keyboard(current_mode_key)

    lines = ["Доступные режимы:"]
    for key, mode in CHAT_MODES.items():
        mark = "✅" if key == current_mode_key else "•"
        lines.append(f"{mark} {mode.title} — {mode.description}")

    await message.answer("\n".join(lines), reply_markup=kb)


# --- Callback-кнопки ---


@router.callback_query(F.data.startswith("mode:"))
async def callback_set_mode(callback: CallbackQuery) -> None:
    assert callback.data is not None
    user = callback.from_user
    assert user is not None

    mode_key = callback.data.split(":", 1)[1]
    if mode_key not in CHAT_MODES:
        await callback.answer("Такого режима нет 🤔", show_alert=True)
        return

    set_mode(user.id, mode_key)
    kb = build_modes_keyboard(mode_key)
    label = get_mode_label(mode_key)

    # Обновляем только клавиатуру, текст приветствия пусть остаётся
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=kb)

    await callback.answer(f"Режим: {label}")


# --- Основной обработчик текста ---


@router.message(F.text & ~F.text.startswith("/"))
async def handle_chat(message: Message) -> None:
    user = message.from_user
    assert user is not None

    # Ограничение по списку разрешённых пользователей
    if settings.allowed_users:
        username = (user.username or "").lower()
        if username not in [u.lower() for u in settings.allowed_users]:
            await message.answer(
                "🚫 Сейчас бот работает в закрытом режиме.\n"
                "Доступ ограничен для тестирования."
            )
            return

    # Rate limit
    ok, _, msg = check_rate_limit(user.id)
    if not ok:
        await message.answer(msg or "⏳ Лимит запросов превышен. Попробуй позже.")
        return

    # Индикация «печатает»
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        try:
            answer = await ask_ai(user.id, message.text or "")
        except Exception:
            logger.exception("Error in handle_chat")
            await message.answer(
                "Кажется, что-то пошло не так на стороне модели 😔\n"
                "Попробуй отправить запрос ещё раз чуть позже."
            )
            return

    for chunk in _split_text(answer):
        await message.answer(chunk)
