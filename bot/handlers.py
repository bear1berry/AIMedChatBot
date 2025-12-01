from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram.utils.chat_action import ChatActionSender
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .ai_client import (
    RateLimitError,
    ask_ai,
    get_state,
    reset_state,
    set_mode,
    get_model_profile_label,  # 🆕 для красивого вывода профиля модели
)
from .modes import CHAT_MODES, DEFAULT_MODE_KEY, get_mode_label, list_modes_for_menu

logger = logging.getLogger(__name__)

router = Router()


def _build_modes_keyboard() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for key, label in list_modes_for_menu().items():
        kb.button(text=label, callback_data=f"set_mode:{key}")
    kb.adjust(1)
    return kb


def _split_text(text: str, max_len: int = 3500) -> list[str]:
    """
    Split long text into chunks that fit into Telegram message limits.
    """
    chunks: list[str] = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break

        split_pos = (
            text.rfind("\n\n", 0, max_len)
            if text.rfind("\n\n", 0, max_len) != -1
            else text.rfind("\n", 0, max_len)
        )
        if split_pos == -1:
            split_pos = text.rfind(" ", 0, max_len)
        if split_pos == -1:
            split_pos = max_len

        chunks.append(text[:split_pos])
        text = text[split_pos:].lstrip()

    return chunks


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user = message.from_user
    assert user is not None

    state = get_state(user.id)
    current_mode_label = get_mode_label(state.mode_key or DEFAULT_MODE_KEY)
    profile_label = get_model_profile_label(state.model_profile)

    kb = _build_modes_keyboard()

    # Минималистичное приветствие в HTML
    text = (
        f"Привет, {user.first_name or 'друг'}! 👋\n\n"
        "Я твой ИИ-ассистент для проекта <b>AI Medicine</b>.\n"
        "Помогу с медицинскими вопросами, идеями для постов и просто поболтать.\n\n"
        f"Режим: <b>{current_mode_label}</b>\n"
        f"Модель: <b>{profile_label}</b>\n\n"
        "✍️ Просто напиши свой вопрос ниже — я отвечу.\n"
        "Чтобы сменить стиль работы или модель, используй кнопки ниже."
    )

    await message.answer(text, reply_markup=kb.as_markup())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "Я ИИ-ассистент, максимально похожий на ChatGPT, но заточенный под твой проект 🧠\n\n"
        "<b>Команды:</b>\n"
        "/start — приветствие и выбор режима\n"
        "/mode — переключить режим общения\n"
        "/reset — очистить историю диалога\n"
        "/help — это сообщение\n\n"
        "Дальше просто общайся со мной обычными сообщениями."
    )
    await message.answer(text)


@router.message(Command("mode"))
async def cmd_mode(message: Message) -> None:
    kb = _build_modes_keyboard()
    await message.answer(
        "Выбери режим работы ассистента:",
        reply_markup=kb.as_markup(),
    )


@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    user = message.from_user
    assert user is not None

    reset_state(user.id)
    await message.answer("История диалога очищена. Начинаем с чистого листа 🧼")


@router.callback_query(F.data.startswith("set_mode:"))
async def callback_set_mode(callback: CallbackQuery) -> None:
    if not callback.data:
        return

    user = callback.from_user
    assert user is not None

    mode_key = callback.data.split(":", 1)[1]

    if mode_key not in CHAT_MODES:
        await callback.answer("Неизвестный режим 🤔", show_alert=True)
        return

    set_mode(user.id, mode_key)
    mode_label = get_mode_label(mode_key)

    # Обновим клавиатуру, чтобы было видно выбранный режим
    await callback.message.edit_reply_markup(
        reply_markup=_build_modes_keyboard().as_markup()
    )
    await callback.answer()
    await callback.message.answer(
        f"Режим переключён на <b>{mode_label}</b>.\n"
        "Можешь задать новый вопрос — контекст старого диалога я обнулил для чистоты ответа."
    )


@router.message(F.photo)
async def photo_handler(message: Message) -> None:
    """
    Обработка фотографий для vision-модели (если включено).
    Сейчас ответ даёт Groq-vision через отдельный модуль.
    """
    from .vision import analyze_image  # локальный импорт, чтобы не ловить циклы

    user = message.from_user
    assert user is not None

    photo = message.photo[-1]
    file = await message.bot.get_file(photo.file_id)
    file_path = file.file_path
    assert file_path is not None

    async with ChatActionSender.upload_photo(chat_id=message.chat.id):
        file_bytes = await message.bot.download_file(file_path)
        content = file_bytes.read()

    async with ChatActionSender.typing(chat_id=message.chat.id):
        reply = await analyze_image(content, user_id=user.id)

    for chunk in _split_text(reply):
        await message.answer(chunk)


@router.message()
async def handle_chat(message: Message) -> None:
    user = message.from_user
    assert user is not None

    text = (message.text or "").strip()
    if not text:
        return

    # Простая защита по username / id (если настроено)
    from .config import settings

    if settings.allowed_users:
        username = (user.username or "").lower()
        if username.lstrip("@") not in {u.lower() for u in settings.allowed_users}:
            await message.answer(
                "Этот бот доступен только для ограниченного круга пользователей. "
                "Если ты считаешь, что это ошибка — напиши администратору."
            )
            return

    try:
        async with ChatActionSender.typing(chat_id=message.chat.id):
            answer = await ask_ai(user_id=user.id, text=text, user_name=user.first_name)
    except RateLimitError as e:
        if e.scope == "minute":
            await message.answer(
                "⏱ Слишком много запросов подряд. "
                "Подожди минуту и попробуй ещё раз."
            )
        else:
            await message.answer(
                "📈 На сегодня лимит запросов исчерпан. "
                "Попробуй снова завтра 🙏"
            )
        return
    except Exception as e:
        logger.exception("Error in handle_chat", exc_info=e)
        await message.answer(
            "Кажется, что-то пошло не так на стороне модели 😔\n"
            "Попробуй отправить запрос ещё раз чуть позже."
        )
        return

    for chunk in _split_text(answer):
        await message.answer(chunk)
