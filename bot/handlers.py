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

    kb = _build_modes_keyboard()

    text = (
        f"Привет, {user.first_name or 'друг'}! 👋\n\n"
        "Я твой ИИ-ассистент на базе GPT-OSS 120B.\n"
        "Помогу с медицинскими вопросами, идеями для постов и просто поболтать.\n\n"
        f"Текущий режим: *{current_mode_label}*\n\n"
        "✍️ Просто напиши свой вопрос ниже — я отвечу.\n"
        "Чтобы сменить стиль работы, нажми кнопку с режимом."
    )

    await message.answer(text, reply_markup=kb.as_markup())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "Я ИИ-ассистент, максимально похожий на ChatGPT, но заточенный под твой проект 🧠\n\n"
        "Доступные команды:\n"
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
    await message.answer("Выбери режим работы ассистента:", reply_markup=kb.as_markup())


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
    mode_key = callback.data.split(":", 1)[1]

    if mode_key not in CHAT_MODES:
        await callback.answer("Неизвестный режим 🤔", show_alert=True)
        return

    set_mode(user.id, mode_key)
    mode_label = get_mode_label(mode_key)

    # обновим клавиатуру (на случай, если ты захочешь подсвечивать выбранный режим)
    await callback.message.edit_reply_markup(
        reply_markup=_build_modes_keyboard().as_markup()
    )
    await callback.answer()
    await callback.message.answer(
        f"Режим переключён на *{mode_label}*.\n"
        "Можешь задать новый вопрос — контекст старого диалога я обнулил для чистоты ответа."
    )


@router.message(F.text & ~F.via_bot)
async def handle_chat(message: Message) -> None:
    user = message.from_user
    assert user is not None

    user_name = user.first_name or user.username or "пользователь"

    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        try:
            answer = await ask_ai(
                user_id=user.id,
                text=message.text,
                user_name=user_name,
            )
        except RateLimitError as e:
            if e.scope == "minute":
                await message.answer(
                    "Слишком много запросов за последнюю минуту 🧨\n"
                    "Попробуй ещё раз через 20–30 секунд."
                )
            else:
                await message.answer(
                    "Достигнут дневной лимит запросов для этого бота 🚫\n"
                    "Лимит обновится завтра."
                )
            return
        except Exception:
            logger.exception("Error in handle_chat")
            await message.answer(
                "Кажется, что-то пошло не так на стороне модели 😔\n"
                "Попробуй отправить запрос ещё раз чуть позже."
            )
            return

    for chunk in _split_text(answer):
        await message.answer(chunk)
