from __future__ import annotations

import logging
from typing import Optional

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram.utils.chat_action import ChatActionSender
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .ai_client import (
    RateLimitError,
    ask_ai,
    continue_answer,
    get_state,
    reset_state,
    transform_last_answer,
    update_preferences,
    set_mode,
)
from .config import settings
from .memory import _get_conn
from .modes import CHAT_MODES, DEFAULT_MODE_KEY, get_mode_label, list_modes_for_menu

logger = logging.getLogger(__name__)

router = Router()

MED_DISCLAIMER = (
    "Это не диагноз и не персональная медицинская рекомендация. "
    "Для оценки состояния и назначения лечения обязательно обратитесь к врачу очно."
)


def _split_text(text: str, max_len: int = 3500) -> list[str]:
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


def _build_modes_keyboard(current_mode: Optional[str] = None) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for key, label in list_modes_for_menu().items():
        mark = "✅" if key == current_mode else "⚪️"
        kb.button(text=f"{mark} {label}", callback_data=f"set_mode:{key}")
    kb.adjust(1)
    return kb


def _build_actions_keyboard() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Конспект", callback_data="act:summary")
    kb.button(text="✍️ Пост для канала", callback_data="act:post")
    kb.button(text="😊 Проще для пациента", callback_data="act:patient")
    kb.adjust(2, 1)
    return kb


def _build_settings_keyboard() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    # verbosity
    kb.button(text="Кратко", callback_data="set_pref:verbosity:short")
    kb.button(text="Нормально", callback_data="set_pref:verbosity:normal")
    kb.button(text="Развернуто", callback_data="set_pref:verbosity:long")
    # tone
    kb.button(text="Нейтрально", callback_data="set_pref:tone:neutral")
    kb.button(text="Дружелюбно", callback_data="set_pref:tone:friendly")
    kb.button(text="Строго", callback_data="set_pref:tone:strict")
    # format
    kb.button(text="Авто", callback_data="set_pref:format:auto")
    kb.button(text="Больше списков", callback_data="set_pref:format:more_lists")
    kb.button(text="Больше текста", callback_data="set_pref:format:more_text")
    kb.adjust(3, 3, 3)
    return kb


def _is_user_allowed(username: Optional[str], user_id: int) -> bool:
    if user_id in settings.admin_ids:
        return True
    if not settings.allowed_users:
        return True
    if not username:
        return False
    username_clean = username.lstrip("@")
    return username_clean in settings.allowed_users


def _detect_emergency(text: str) -> bool:
    t = text.lower()
    keywords = [
        "сильная боль в груди",
        "сильная давящая боль в груди",
        "не могу дышать",
        "трудно дышать",
        "задыхаюсь",
        "одышка в покое",
        "не чувствую руку",
        "не чувствую ногу",
        "кривит лицо",
        "перекосило лицо",
        "не может говорить",
        "не отвечает на слова",
        "потерял сознание",
        "не приходит в сознание",
        "обильное кровотечение",
        "кровь изо рта",
        "рвота с кровью",
        "чёрный стул",
        "чёрная рвота",
        "судороги впервые",
        "припадок впервые",
        "температура 40",
        "температура 41",
        "давление 220",
        "давление 200",
        "давление 240",
    ]
    for kw in keywords:
        if kw in t:
            return True
    return False


def _emergency_prefix() -> str:
    return (
        "⚠️ По описанию могут быть признаки опасного состояния. "
        "Онлайн-бот не подходит для неотложной помощи. Если самочувствие тяжёлое, "
        "немедленно вызовите скорую помощь (103/112) или обратитесь в ближайший стационар.\n\n"
    )


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user = message.from_user
    assert user is not None

    if not _is_user_allowed(user.username, user.id):
        await message.answer(
            "Бот сейчас в закрытом тесте и доступен только ограниченному кругу пользователей."
        )
        return

    state = get_state(user.id)
    current_mode_label = get_mode_label(state.mode_key or DEFAULT_MODE_KEY)

    kb = _build_modes_keyboard(current_mode=state.mode_key)

    text = (
        f"Привет, {user.first_name or 'друг'}! 👋\n\n"
        "Я твой ИИ-ассистент на базе моделей Groq (GPT-OSS).\n"
        "Помогу с медицинскими вопросами, идеями для постов и просто поболтать.\n\n"
        f"Текущий режим: *{current_mode_label}*\n\n"
        "✍️ Просто напиши свой вопрос ниже — я отвечу.\n"
        "Чтобы сменить стиль работы, нажми кнопку с режимом или команду /mode."
    )

    await message.answer(text, reply_markup=kb.as_markup())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "Я ИИ-ассистент, максимально похожий на ChatGPT, но заточенный под твой проект 🧠\n\n"
        "Команды:\n"
        "/start — приветствие и выбор режима\n"
        "/mode — переключить режим общения\n"
        "/settings — настройки стиля ответа\n"
        "/reset — очистить историю диалога\n"
        "/ping — проверить, жив ли бот\n"
        "/health — проверить доступность модели\n"
        "/help — это сообщение\n\n"
        "Дальше просто общайся со мной обычными сообщениями."
    )


@router.message(Command("mode"))
async def cmd_mode(message: Message) -> None:
    user = message.from_user
    assert user is not None

    if not _is_user_allowed(user.username, user.id):
        await message.answer("Бот сейчас в закрытом тесте и доступен только ограниченному кругу пользователей.")
        return

    state = get_state(user.id)
    kb = _build_modes_keyboard(current_mode=state.mode_key)
    await message.answer("Выбери режим работы ассистента:", reply_markup=kb.as_markup())


@router.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    user = message.from_user
    assert user is not None

    if not _is_user_allowed(user.username, user.id):
        await message.answer("Бот сейчас в закрытом тесте и доступен только ограниченному кругу пользователей.")
        return

    state = get_state(user.id)
    text = (
        "Настройки формата ответа:\n"
        f"- Длина: *{state.verbosity}*\n"
        f"- Тон: *{state.tone}*\n"
        f"- Формат: *{state.format_pref}*\n\n"
        "Выбери новые параметры:"
    )
    kb = _build_settings_keyboard()
    await message.answer(text, reply_markup=kb.as_markup())


@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    user = message.from_user
    assert user is not None

    if not _is_user_allowed(user.username, user.id):
        await message.answer("Бот сейчас в закрытом тесте и доступен только ограниченному кругу пользователей.")
        return

    reset_state(user.id)
    await message.answer("История диалога очищена. Начинаем с чистого листа 🧼")


@router.message(Command("ping"))
async def cmd_ping(message: Message) -> None:
    await message.answer("Я на связи ✅")


@router.message(Command("health"))
async def cmd_health(message: Message) -> None:
    from .ai_client import healthcheck_llm

    ok = await healthcheck_llm()
    if ok:
        await message.answer("LLM отвечает нормально ✅")
    else:
        await message.answer("LLM сейчас не отвечает или есть проблемы с подключением ⚠️")


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    user = message.from_user
    assert user is not None

    if user.id not in settings.admin_ids:
        await message.answer("Эта команда доступна только админу.")
        return

    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM conversations")
    users_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM rate_limits")
    rl_rows = cur.fetchone()[0]
    cur.close()

    await message.answer(
        "Статистика бота:\n"
        f"- Пользователей с сохранённой историей: *{users_count}*\n"
        f"- Записей в таблице rate_limits: *{rl_rows}*"
    )


@router.callback_query(F.data.startswith("set_mode:"))
async def callback_set_mode(callback: CallbackQuery) -> None:
    if not callback.data:
        return

    user = callback.from_user
    assert user is not None

    if not _is_user_allowed(user.username, user.id):
        await callback.answer("Бот в закрытом тесте.", show_alert=True)
        return

    mode_key = callback.data.split(":", 1)[1]

    if mode_key not in CHAT_MODES:
        await callback.answer("Неизвестный режим 🤔", show_alert=True)
        return

    set_mode(user.id, mode_key)
    mode_label = get_mode_label(mode_key)

    await callback.message.edit_reply_markup(
        reply_markup=_build_modes_keyboard(current_mode=mode_key).as_markup()
    )
    await callback.answer()
    await callback.message.answer(
        f"Режим переключён на *{mode_label}*.\n"
        "Можешь задать новый вопрос — контекст старого диалога я обнулил для чистоты ответа."
    )


@router.callback_query(F.data.startswith("set_pref:"))
async def callback_set_pref(callback: CallbackQuery) -> None:
    if not callback.data:
        return
    user = callback.from_user
    assert user is not None

    if not _is_user_allowed(user.username, user.id):
        await callback.answer("Бот в закрытом тесте.", show_alert=True)
        return

    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("Некорректные данные.", show_alert=True)
        return

    _, pref_type, value = parts
    kwargs = {}
    if pref_type == "verbosity":
        kwargs["verbosity"] = value
    elif pref_type == "tone":
        kwargs["tone"] = value
    elif pref_type == "format":
        kwargs["format_pref"] = value
    else:
        await callback.answer("Неизвестный параметр.", show_alert=True)
        return

    state = update_preferences(user.id, **kwargs)
    await callback.answer("Настройки обновлены ✅", show_alert=False)

    text = (
        "Текущие настройки:\n"
        f"- Длина: *{state.verbosity}*\n"
        f"- Тон: *{state.tone}*\n"
        f"- Формат: *{state.format_pref}*"
    )
    await callback.message.edit_text(text, reply_markup=_build_settings_keyboard().as_markup())


@router.callback_query(F.data.startswith("act:"))
async def callback_actions(callback: CallbackQuery) -> None:
    if not callback.data:
        return
    user = callback.from_user
    assert user is not None

    if not _is_user_allowed(user.username, user.id):
        await callback.answer("Бот в закрытом тесте.", show_alert=True)
        return

    kind = callback.data.split(":", 1)[1]

    async with ChatActionSender.typing(bot=callback.message.bot, chat_id=callback.message.chat.id):
        try:
            result = await transform_last_answer(user.id, user.first_name or user.username, kind)
        except RateLimitError as e:
            await callback.answer("Превышен лимит запросов.", show_alert=True)
            if e.message:
                await callback.message.answer(e.message)
            return
        except ValueError as e:
            await callback.answer(str(e), show_alert=True)
            return
        except Exception:
            logger.exception("Error in callback_actions")
            await callback.message.answer(
                "Кажется, что-то пошло не так на стороне модели 😔\n"
                "Попробуй ещё раз чуть позже."
            )
            return

    for chunk in _split_text(result):
        await callback.message.answer(chunk)


@router.message(F.text & ~F.via_bot)
async def handle_chat(message: Message) -> None:
    user = message.from_user
    assert user is not None

    if not _is_user_allowed(user.username, user.id):
        await message.answer(
            "Бот сейчас в закрытом тесте и доступен только ограниченному кругу пользователей."
        )
        return

    user_name = user.first_name or user.username or "пользователь"
    text = message.text or ""

    simple = text.strip().lower()
    wants_continue = simple in {"ещё", "еще", "продолжи", "продолжай", "дальше"}

    emergency = _detect_emergency(text)
    state = get_state(user.id)
    is_med_mode = state.mode_key == "ai_medicine_assistant"

    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        try:
            if wants_continue:
                answer = await continue_answer(user.id, user_name=user_name)
            else:
                answer = await ask_ai(user_id=user.id, text=text, user_name=user_name)
        except RateLimitError as e:
            logger.info("Rate limit for user %s: %s", user.id, e.message)
            await message.answer(e.message)
            return
        except Exception:
            logger.exception("Error in handle_chat")
            await message.answer(
                "Кажется, что-то пошло не так на стороне модели 😔\n"
                "Попробуй отправить запрос ещё раз чуть позже."
            )
            return

    if is_med_mode:
        if emergency:
            answer = _emergency_prefix() + answer
        if "это не диагноз" not in answer.lower():
            answer = answer.rstrip() + "\n\n_" + MED_DISCLAIMER + "_"

    kb_actions = _build_actions_keyboard().as_markup()

    chunks = _split_text(answer)
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        if is_last:
            await message.answer(chunk, reply_markup=kb_actions)
        else:
            await message.answer(chunk)
