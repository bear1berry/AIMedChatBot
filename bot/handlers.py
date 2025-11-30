from __future__ import annotations

import logging
from datetime import datetime
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
from .memory import _get_conn, create_case, list_cases, create_note, list_notes, search_notes
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


def _build_actions_keyboard(include_case: bool = False) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Конспект", callback_data="act:summary")
    kb.button(text="✍️ Пост для канала", callback_data="act:post")
    kb.button(text="😊 Проще для пациента", callback_data="act:patient")
    if include_case:
        kb.button(text="💾 Сохранить как кейс", callback_data="act:case")
        kb.adjust(2, 2)
    else:
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


def _build_menu_keyboard() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔁 Сменить режим", callback_data="menu:mode")
    kb.button(text="⚙️ Настройки ответа", callback_data="menu:settings")
    kb.button(text="💾 Сохранить кейс", callback_data="menu:case")
    kb.button(text="✍️ Пост из ответа", callback_data="menu:post")
    kb.adjust(2, 2)
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


RED_KEYWORDS = [
    "сильная боль в груди",
    "сильная давящая боль в груди",
    "острая боль в груди",
    "не могу дышать",
    "трудно дышать",
    "задыхаюсь",
    "одышка в покое",
    "внезапная одышка",
    "не чувствую руку",
    "не чувствую ногу",
    "кривит лицо",
    "перекосило лицо",
    "онемение половины тела",
    "не может говорить",
    "не отвечает на слова",
    "внезапная слабость в руке",
    "внезапная слабость в ноге",
    "потерял сознание",
    "не приходит в сознание",
    "обморок",
    "обильное кровотечение",
    "сильное кровотечение",
    "кровь изо рта",
    "рвота с кровью",
    "черный стул",
    "чёрный стул",
    "чёрная рвота",
    "черная рвота",
    "судороги впервые",
    "припадок впервые",
    "приступ судорог",
    "температура 40",
    "температура 41",
    "давление 220",
    "давление 230",
    "давление 240",
]


YELLOW_KEYWORDS = [
    "кровь в стуле",
    "кровь в моче",
    "кровохаркание",
    "сильная головная боль",
    "сильная головная боль впервые",
    "резкая головная боль",
    "снижение веса",
    "сильно похудел",
    "сильное похудение",
    "рвота больше суток",
    "понос больше суток",
    "задержка мочи",
    "задержка стула",
    "сильная боль в животе",
    "сильная боль в пояснице",
]


def _detect_emergency(text: str) -> Optional[str]:
    """
    Определяет уровень срочности:
        - 'red' — возможное экстренное состояние
        - 'yellow' — настоятельно рекомендуется очный осмотр в ближайшее время
        - None — особых фраз-триггеров нет
    """
    t = text.lower()
    for kw in RED_KEYWORDS:
        if kw in t:
            return "red"
    for kw in YELLOW_KEYWORDS:
        if kw in t:
            return "yellow"
    return None


def _emergency_prefix(level: str) -> str:
    if level == "red":
        return (
            "⚠️ По описанию могут быть признаки опасного для жизни состояния. "
            "Онлайн-бот не подходит для такой ситуации. Немедленно вызовите скорую помощь (103/112) "
            "или обратитесь в ближайший стационар.\n\n"
        )
    if level == "yellow":
        return (
            "⚠️ Описание симптомов требует очной оценки врача в ближайшее время. "
            "Онлайн-бот не может заменить полноценный осмотр. Постарайтесь как можно скорее "
            "обратиться к врачу.\n\n"
        )
    return ""


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
        "/menu — общее меню\n"
        "/settings — настройки стиля ответа\n"
        "/case — сохранить текущий диалог как клинический случай\n"
        "/cases — список сохранённых случаев\n"
        "/note — сохранить ответ как заметку\n"
        "/notes — список заметок\n"
        "/findnote — поиск по заметкам\n"
        "/reset — очистить историю диалога\n"
        "/ping — проверить, жив ли бот\n"
        "/health — проверить доступность модели\n"
        "/stats — статистика (только админ)\n"
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


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    user = message.from_user
    assert user is not None

    if not _is_user_allowed(user.username, user.id):
        await message.answer("Бот сейчас в закрытом тесте и доступен только ограниченному кругу пользователей.")
        return

    state = get_state(user.id)
    mode_label = get_mode_label(state.mode_key)
    text = (
        "Меню ассистента:\n\n"
        f"- Текущий режим: *{mode_label}*\n"
        f"- Длина ответа: *{state.verbosity}*\n"
        f"- Тон: *{state.tone}*\n"
        f"- Формат: *{state.format_pref}*\n\n"
        "Выбери действие:"
    )
    kb = _build_menu_keyboard()
    await message.answer(text, reply_markup=kb.as_markup())


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
    cur.execute("SELECT COUNT(*) FROM cases")
    cases_rows = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM notes")
    notes_rows = cur.fetchone()[0]
    cur.close()

    await message.answer(
        "Статистика бота:\n"
        f"- Пользователей с сохранённой историей: *{users_count}*\n"
        f"- Записей в rate_limits: *{rl_rows}*\n"
        f"- Сохранённых клинических случаев: *{cases_rows}*\n"
        f"- Заметок: *{notes_rows}*"
    )


@router.message(Command("case"))
async def cmd_case(message: Message) -> None:
    user = message.from_user
    assert user is not None

    if not _is_user_allowed(user.username, user.id):
        await message.answer("Бот сейчас в закрытом тесте и доступен только ограниченному кругу пользователей.")
        return

    args = (message.text or "").split(maxsplit=1)
    if len(args) > 1:
        title = args[1].strip()
    else:
        now = datetime.now()
        title = now.strftime("Клинический случай от %Y-%m-%d %H:%M")

    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        try:
            summary = await transform_last_answer(user.id, user.first_name or user.username, kind="case")
        except RateLimitError as e:
            await message.answer(e.message)
            return
        except ValueError as e:
            await message.answer(str(e))
            return
        except Exception:
            logger.exception("Error in /case")
            await message.answer(
                "Не удалось сформировать кейс из последнего ответа. Попробуй ещё раз позже."
            )
            return

    case_id = create_case(user.id, title, summary)
    text = f"Клинический случай сохранён под номером #{case_id}.\n\n*{title}*\n\n{summary}"
    for chunk in _split_text(text):
        await message.answer(chunk)


@router.message(Command("cases"))
async def cmd_cases(message: Message) -> None:
    user = message.from_user
    assert user is not None

    if not _is_user_allowed(user.username, user.id):
        await message.answer("Бот сейчас в закрытом тесте и доступен только ограниченному кругу пользователей.")
        return

    cases = list_cases(user.id, limit=10)
    if not cases:
        await message.answer("Пока нет сохранённых клинических случаев.")
        return

    lines: list[str] = ["Последние сохранённые случаи:"]
    for row in cases:
        dt = datetime.fromtimestamp(row["created_at"])
        date_str = dt.strftime("%Y-%m-%d %H:%M")
        preview = row["summary"].strip().replace("\n", " ")
        if len(preview) > 120:
            preview = preview[:117] + "..."
        lines.append(
            f"\n• #{row['id']} — *{row['title']}* ({date_str})\n  {preview}"
        )

    await message.answer("\n".join(lines))


@router.message(Command("note"))
async def cmd_note(message: Message) -> None:
    user = message.from_user
    assert user is not None

    if not _is_user_allowed(user.username, user.id):
        await message.answer("Бот сейчас в закрытом тесте и доступен только ограниченному кругу пользователей.")
        return

    args = (message.text or "").split(maxsplit=1)
    title = args[1].strip() if len(args) > 1 else "Заметка"

    state = get_state(user.id)
    body = state.last_answer or state.last_question
    if not body:
        await message.answer(
            "Пока нечего сохранять как заметку — сначала получи от меня ответ или отправь текст."
        )
        return

    note_id = create_note(user.id, title, body)
    await message.answer(f"Заметка #{note_id} сохранена.\n\n*{title}*")


@router.message(Command("notes"))
async def cmd_notes(message: Message) -> None:
    user = message.from_user
    assert user is not None

    if not _is_user_allowed(user.username, user.id):
        await message.answer("Бот сейчас в закрытом тесте и доступен только ограниченному кругу пользователей.")
        return

    notes = list_notes(user.id, limit=10)
    if not notes:
        await message.answer("У тебя пока нет сохранённых заметок.")
        return

    lines: list[str] = ["Последние заметки:"]
    for row in notes:
        dt = datetime.fromtimestamp(row["created_at"])
        date_str = dt.strftime("%Y-%m-%d %H:%M")
        preview = row["body"].strip().replace("\n", " ")
        if len(preview) > 120:
            preview = preview[:117] + "..."
        lines.append(
            f"\n• #{row['id']} — *{row['title']}* ({date_str})\n  {preview}"
        )

    await message.answer("\n".join(lines))


@router.message(Command("findnote"))
async def cmd_findnote(message: Message) -> None:
    user = message.from_user
    assert user is not None

    if not _is_user_allowed(user.username, user.id):
        await message.answer("Бот сейчас в закрытом тесте и доступен только ограниченному кругу пользователей.")
        return

    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.answer(
            "Укажи текст для поиска, например:\n"
            "`/findnote гипертония`",
        )
        return

    query = args[1].strip()
    notes = search_notes(user.id, query=query, limit=10)
    if not notes:
        await message.answer("По этому запросу заметок не найдено.")
        return

    lines: list[str] = [f"Заметки по запросу *{query}*:"]
    for row in notes:
        dt = datetime.fromtimestamp(row["created_at"])
        date_str = dt.strftime("%Y-%m-%d %H:%M")
        preview = row["body"].strip().replace("\n", " ")
        if len(preview) > 120:
            preview = preview[:117] + "..."
        lines.append(
            f"\n• #{row['id']} — *{row['title']}* ({date_str})\n  {preview}"
        )

    await message.answer("\n".join(lines))


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
            if kind == "case":
                summary = await transform_last_answer(
                    user.id, user.first_name or user.username, kind="case"
                )
                now = datetime.now()
                title = now.strftime("Клинический случай от %Y-%m-%d %H:%M")
                case_id = create_case(user.id, title, summary)
                result = f"Клинический случай сохранён под номером #{case_id}.\n\n*{title}*\n\n{summary}"
            else:
                result = await transform_last_answer(
                    user.id, user.first_name or user.username, kind=kind
                )
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


@router.callback_query(F.data.startswith("menu:"))
async def callback_menu(callback: CallbackQuery) -> None:
    if not callback.data:
        return
    user = callback.from_user
    assert user is not None

    if not _is_user_allowed(user.username, user.id):
        await callback.answer("Бот в закрытом тесте.", show_alert=True)
        return

    action = callback.data.split(":", 1)[1]

    if action == "mode":
        state = get_state(user.id)
        kb = _build_modes_keyboard(current_mode=state.mode_key)
        await callback.message.answer("Выбери режим работы ассистента:", reply_markup=kb.as_markup())
        await callback.answer()
        return

    if action == "settings":
        state = get_state(user.id)
        text = (
            "Настройки формата ответа:\n"
            f"- Длина: *{state.verbosity}*\n"
            f"- Тон: *{state.tone}*\n"
            f"- Формат: *{state.format_pref}*\n\n"
            "Выбери новые параметры:"
        )
        kb = _build_settings_keyboard()
        await callback.message.answer(text, reply_markup=kb.as_markup())
        await callback.answer()
        return

    if action in {"case", "post"}:
        kind = "case" if action == "case" else "post"
        async with ChatActionSender.typing(bot=callback.message.bot, chat_id=callback.message.chat.id):
            try:
                if kind == "case":
                    summary = await transform_last_answer(
                        user.id, user.first_name or user.username, kind="case"
                    )
                    now = datetime.now()
                    title = now.strftime("Клинический случай от %Y-%m-%d %H:%M")
                    case_id = create_case(user.id, title, summary)
                    result = f"Клинический случай сохранён под номером #{case_id}.\n\n*{title}*\n\n{summary}"
                else:
                    result = await transform_last_answer(
                        user.id, user.first_name or user.username, kind="post"
                    )
            except RateLimitError as e:
                await callback.answer("Превышен лимит запросов.", show_alert=True)
                if e.message:
                    await callback.message.answer(e.message)
                return
            except ValueError as e:
                await callback.answer(str(e), show_alert=True)
                return
            except Exception:
                logger.exception("Error in callback_menu")
                await callback.message.answer(
                    "Кажется, что-то пошло не так на стороне модели 😔\n"
                    "Попробуй ещё раз чуть позже."
                )
                return

        for chunk in _split_text(result):
            await callback.message.answer(chunk)

        await callback.answer()
        return

    await callback.answer("Неизвестное действие меню.", show_alert=True)


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

    state = get_state(user.id)
    is_med_mode = state.mode_key == "ai_medicine_assistant"
    emergency_level = _detect_emergency(text) if is_med_mode else None

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
        if emergency_level:
            answer = _emergency_prefix(emergency_level) + answer
        if "это не диагноз" not in answer.lower():
            answer = answer.rstrip() + "\n\n_" + MED_DISCLAIMER + "_"

    kb_actions = _build_actions_keyboard(include_case=is_med_mode).as_markup()

    chunks = _split_text(answer)
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        if is_last:
            await message.answer(chunk, reply_markup=kb_actions)
        else:
            await message.answer(chunk)
