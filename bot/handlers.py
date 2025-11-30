from __future__ import annotations

import logging
from typing import Dict

from aiogram import Router, F, Bot
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .ai_client import ask_ai
from .config import settings
from .keyboards import main_menu_keyboard, modes_keyboard, answer_with_modes_keyboard
from .memory import (
    create_conversation,
    get_history,
    get_profile,
    get_stats,
    list_conversations,
    register_user,
    save_message,
    set_profile,
)
from .modes import MODES, detect_mode
from .vision import analyze_image

logger = logging.getLogger(__name__)

router = Router(name="main")

# Текущее текстовое режимное состояние в памяти процесса
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


# ====== FSM для профиля ======
class ProfileStates(StatesGroup):
    waiting_profile = State()


# ====== FSM для симптом-чекера ======
class SymptomStates(StatesGroup):
    symptom = State()
    duration = State()
    details = State()
    red_flags = State()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user = message.from_user
    if not user:
        return

    if not _is_allowed(user.username):
        await message.answer(
            "🚫 Этот бот доступен ограниченному кругу пользователей.\n"
            "Если нужен доступ — напишите владельцу."
        )
        return

    register_user(user.id, user.username)
    create_conversation(user.id, "Диалог")
    _set_user_mode(user.id, "default")

    text = (
        "👋 Привет, я <b>AI Medicine Assistant</b> — твой умный мед-помощник.\n\n"
        "✨ Что я умею:\n"
        "• разбирать симптомы и объяснять, что может происходить;\n"
        "• помогать интерпретировать анализы и заключения;\n"
        "• подсказывать, к какому врачу и с чем идти;\n"
        "• помогать тебе как врачу: структурировать случаи, готовить тексты, памятки.\n\n"
        "Выбери действие ниже или просто напиши свой вопрос 👇"
    )

    await message.answer(text, reply_markup=main_menu_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "🧭 <b>Навигация</b>\n\n"
        "• /mode — выбрать профиль ответа (общий, симптомы, педиатрия и др.)\n"
        "• /symptoms — запустить пошаговый симптом-чекер\n"
        "• /profile — настроить твой профиль (пациент / врач / студент и т.п.)\n"
        "• /new — начать новый «кейс» / диалог\n"
        "• /cases — список последних кейсов\n"
        "• /stats — общая статистика (для админа)\n\n"
        "Или просто напиши свой вопрос — я сам подберу подходящий режим 🤖"
    )


@router.message(Command("mode"))
async def cmd_mode(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    if not _is_allowed(user.username):
        await message.answer("🚫 Доступ запрещён.")
        return
    current = _get_user_mode(user.id)
    await message.answer(
        "Выбери режим работы ИИ 🧠:",
        reply_markup=modes_keyboard(current),
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

    _set_user_mode(user.id, mode)
    cfg = MODES[mode]
    try:
        if callback.message:
            await callback.message.edit_reply_markup(
                reply_markup=modes_keyboard(mode),
            )
    except Exception as e:
        logger.debug("edit_reply_markup failed: %s", e)

    await callback.answer(f"Режим: {cfg['short_name']}")


# ==== Главное меню (callback) ====

@router.callback_query(F.data == "menu:symptoms")
async def cb_menu_symptoms(callback: CallbackQuery, state: FSMContext) -> None:
    await cmd_symptoms_start(callback.message, state)
    await callback.answer()


@router.callback_query(F.data == "menu:profile")
async def cb_menu_profile(callback: CallbackQuery, state: FSMContext) -> None:
    await cmd_profile(callback.message, state)
    await callback.answer()


@router.callback_query(F.data == "menu:cases")
async def cb_menu_cases(callback: CallbackQuery) -> None:
    await cmd_cases(callback.message)
    await callback.answer()


@router.callback_query(F.data == "menu:ask")
async def cb_menu_ask(callback: CallbackQuery) -> None:
    await callback.message.answer("Напиши свой вопрос 👇")
    await callback.answer()


# ===== Профиль =====

@router.message(Command("profile"))
async def cmd_profile(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if not user:
        return

    profile = get_profile(user.id)
    await state.set_state(ProfileStates.waiting_profile)

    await message.answer(
        "🧑‍⚕️ <b>Профиль пользователя</b>\n\n"
        f"Текущие настройки:\n"
        f"• Роль: <code>{profile['role']}</code>\n"
        f"• Детализация: <code>{profile['detail_level']}</code>\n"
        f"• Юрисдикция: <code>{profile['jurisdiction']}</code>\n\n"
        "Отправь одно сообщение в формате:\n"
        "<code>роль, детализация, юрисдикция</code>\n\n"
        "Примеры:\n"
        "• <i>врач, подробно, РФ</i>\n"
        "• <i>пациент, стандарт, GLOBAL<
