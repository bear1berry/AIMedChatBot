from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.chat_action import ChatActionSender
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .ai_client import (
    RateLimitError,
    ask_ai,
    get_state,
    reset_state,
    set_mode,
    set_model_profile,
    get_model_profile_label,
)
from .modes import CHAT_MODES, DEFAULT_MODE_KEY, get_mode_label, list_modes_for_menu

logger = logging.getLogger(__name__)

router = Router()


# =========================
# ВСПОМОГАТЕЛЬНЫЕ КЛАВИАТУРЫ
# =========================


def _build_modes_keyboard(current_mode: str) -> InlineKeyboardBuilder:
    """
    Кнопки выбора режима ассистента (AI-Medicine, универсальный ассистент,
    собеседник, контент-режим и т.п.).
    """
    kb = InlineKeyboardBuilder()

    for key, label in list_modes_for_menu().items():
        mark = "✅" if key == current_mode else "⚪️"
        kb.button(text=f"{mark} {label}", callback_data=f"set_mode:{key}")
    kb.adjust(1)

    # Отдельная кнопка запуска мастера мед.запроса, если режим существует
    if "ai_medicine_assistant" in CHAT_MODES:
        kb.row(
            InlineKeyboardButton(
                text="🩺 Медицинский запрос по шагам",
                callback_data="wizard:med",
            )
        )

    return kb


def _build_models_keyboard(current_profile: str) -> InlineKeyboardBuilder:
    """
    Кнопки выбора профиля модели (авто, GPT-4.1, mini, OSS, DeepSeek и т.д.).
    """
    kb = InlineKeyboardBuilder()
    profiles = [
        ("auto", "🤖 Авто (подбор моделей)"),
        ("gpt4", "🧠 GPT-4.1"),
        ("mini", "⚡️ GPT-4o mini"),
        ("oss", "🧬 GPT-OSS 120B"),
        ("deepseek_reasoner", "🧩 DeepSeek Reasoner"),
        ("deepseek_chat", "💬 DeepSeek Chat"),
    ]
    for code, label in profiles:
        mark = "✅" if code == current_profile else "⚪️"
        kb.button(text=f"{mark} {label}", callback_data=f"set_model:{code}")
    kb.adjust(1)
    return kb


def _build_answer_actions_keyboard() -> InlineKeyboardBuilder:
    """
    Кнопки под ответом: упростить, сделать чек-лист, раскрыть подробнее.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="🔁 Сказать проще", callback_data="ans:simplify")
    kb.button(text="📌 Чек-лист действий", callback_data="ans:checklist")
    kb.button(text="🔍 Раскрыть подробнее", callback_data="ans:expand")
    kb.adjust(1)
    return kb


def _split_text(text: str, max_len: int = 3500) -> list[str]:
    """
    Аккуратно режем длинный текст на куски под лимит Telegram.
    Стараемся резать по пустой строке / строке / пробелу.
    """
    chunks: list[str] = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break

        split_pos = text.rfind("\n\n", 0, max_len)
        if split_pos == -1:
            split_pos = text.rfind("\n", 0, max_len)
        if split_pos == -1:
            split_pos = text.rfind(" ", 0, max_len)
        if split_pos == -1:
            split_pos = max_len

        chunks.append(text[:split_pos])
        text = text[split_pos:].lstrip()

    return chunks


# =========================
# МАСТЕР МЕДИЦИНСКОГО ЗАПРОСА
# =========================


def _get_wizard_state(user_id: int) -> Optional[Dict[str, Any]]:
    """
    Забираем состояние мастера из ConversationState.
    Используем динамический атрибут `wizard` на объекте state.
    """
    state = get_state(user_id)
    return getattr(state, "wizard", None)


def _set_wizard_state(user_id: int, wizard: Optional[Dict[str, Any]]) -> None:
    state = get_state(user_id)
    if wizard is None:
        if hasattr(state, "wizard"):
            delattr(state, "wizard")
    else:
        state.wizard = wizard  # type: ignore[attr-defined]


async def _start_med_wizard(message: Message) -> None:
    """
    Запустить мастер пошагового медицинского запроса.
    """
    user = message.from_user
    if user is None:
        return

    # Гарантируем медицинский режим
    if "ai_medicine_assistant" in CHAT_MODES:
        set_mode(user.id, "ai_medicine_assistant")

    _set_wizard_state(
        user.id,
        {
            "type": "med",
            "step": 1,
            "data": {},
        },
    )

    text = (
        "🩺 <b>Мастер медицинского запроса</b>\n\n"
        "Я задам несколько вопросов, чтобы собрать картину, а затем дам структурированный разбор.\n\n"
        "<b>[1/4]</b> Укажи возраст и пол.\n"
        "Например: <code>28 лет, мужчина</code>."
    )
    await message.answer(text)


async def _process_med_wizard(message: Message, wizard: Dict[str, Any]) -> None:
    """
    Обработка шагов мастера.
    """
    user = message.from_user
    if user is None:
        return

    step = int(wizard.get("step", 1))
    data: Dict[str, str] = wizard.setdefault("data", {})

    text = (message.text or "").strip()

    # Шаг 1: возраст и пол
    if step == 1:
        data["age_sex"] = text
        wizard["step"] = 2
        _set_wizard_state(user.id, wizard)
        await message.answer(
            "<b>[2/4]</b> Опиши основные жалобы:\n"
            "• что именно беспокоит;\n"
            "• как давно началось;\n"
            "• что усиливает или облегчает симптомы."
        )
        return

    # Шаг 2: жалобы
    if step == 2:
        data["symptoms"] = text
        wizard["step"] = 3
        _set_wizard_state(user.id, wizard)
        await message.answer(
            "<b>[3/4]</b> Напиши известные диагнозы и хронические заболевания (если есть).\n"
            "Если ничего не знаешь — так и напиши: <code>не знаю</code> или <code>не обращался</code>."
        )
        return

    # Шаг 3: анамнез
    if step == 3:
        data["history"] = text
        wizard["step"] = 4
        _set_wizard_state(user.id, wizard)
        await message.answer(
            "<b>[4/4]</b> Перечисли лекарства, которые принимаешь (или принимал недавно), "
            "и какие обследования уже выполнялись (анализы, УЗИ, МРТ и т.п.).\n"
            "Если ничего не было — так и напиши."
        )
        return

    # Шаг 4: лечение/обследования – завершаем мастер и отправляем запрос в ИИ
    if step == 4:
        data["treatment"] = text
        _set_wizard_state(user.id, None)

        # Собираем сводный запрос
        user_name = user.first_name or user.username or ""
        prompt = (
            "Сформируй структурированный медицинский разбор на основе следующих данных:\n\n"
            f"1) Возраст и пол: {data.get('age_sex', '—')}\n"
            f"2) Основные жалобы и длительность: {data.get('symptoms', '—')}\n"
            f"3) Диагнозы / хронические заболевания: {data.get('history', '—')}\n"
            f"4) Принимаемые лекарства и уже выполненные обследования: {data.get('treatment', '—')}\n\n"
            "Дай ответ на русском языке.\n\n"
            "Структура ответа:\n"
            "1. 💡 <b>Кратко</b> — 2–4 предложения с общей картиной.\n"
            "2. 🧬 <b>Возможные объяснения</b> — краткий список возможных причин/механизмов.\n"
            "3. 📋 <b>Что можно сделать сейчас</b> — 3–7 пунктов простых, безопасных шагов "
            "(без назначения рецептурных препаратов).\n"
            "4. ⚠️ <b>Когда срочно к врачу</b> — отдельный блок с чёткими критериями.\n"
            "5. 💬 <b>Что обсудить с врачом</b> — список вопросов и возможных обследований.\n\n"
            "Не ставь окончательный диагноз, не давай индивидуального плана лечения. "
            "Подчеркни, что это не заменяет очный приём."
        )

        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            try:
                answer = await ask_ai(
                    user_id=user.id,
                    text=prompt,
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
                logger.exception("Error in med wizard ask_ai")
                await message.answer(
                    "Кажется, что-то пошло не так при обработке медицинского запроса 😔\n"
                    "Попробуй ещё раз сформулировать вопрос или задать его заново."
                )
                return

        kb_actions = _build_answer_actions_keyboard()

        chunks = _split_text(answer)
        if not chunks:
            return

        # К первому блоку цепляем кнопки управления
        await message.answer(chunks[0], reply_markup=kb_actions.as_markup())
        for chunk in chunks[1:]:
            await message.answer(chunk)

        return

    # На всякий случай – если step неожиданно вне диапазона
    _set_wizard_state(user.id, None)
    await message.answer(
        "Мастер медицинского запроса сброшен из-за непредвидённой ошибки.\n"
        "Попробуй запустить его снова через кнопку «🩺 Медицинский запрос по шагам»."
    )


# =========================
# КОМАНДЫ
# =========================


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """
    Приветствие + главное меню: режимы + профиль модели + кнопка мастера.
    """
    user = message.from_user
    if user is None:
        return

    state = get_state(user.id)
    current_mode = state.mode_key or DEFAULT_MODE_KEY
    current_mode_label = get_mode_label(current_mode)
    current_profile_label = get_model_profile_label(state.model_profile)

    kb_modes = _build_modes_keyboard(current_mode=current_mode)
    kb_models = _build_models_keyboard(current_profile=state.model_profile)
    kb_modes.attach(kb_models)

    text = (
        f"Привет, {user.first_name or 'друг'}! 👋\n\n"
        "<b>AIMed</b> — твой ИИ-ассистент для проекта <b>AI Medicine</b> и личного развития.\n\n"
        "Что я могу:\n"
        "• ⚕️ Разобрать симптомы и анализы, помочь подготовиться к приёму (без постановки диагноза).\n"
        "• 📚 Помочь с учёбой, экзаменами и разбором сложных тем.\n"
        "• ✍️ Придумать и допилить контент для Telegram-каналов.\n"
        "• 🧠 Поддержать, навести порядок в голове и помочь выстроить план действий.\n\n"
        "Текущие настройки:\n"
        f"• Режим: <b>{current_mode_label}</b>\n"
        f"• Модель: <b>{current_profile_label}</b>\n\n"
        "✍️ Просто напиши свой запрос ниже — я подстроюсь под контекст.\n"
        "Или начни с кнопок: выбери режим, модель или запусти мастер мед.запроса 👇"
    )

    await message.answer(text, reply_markup=kb_modes.as_markup())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """
    Краткая справка по возможностям и командам.
    """
    text = (
        "Я ИИ-ассистент, максимально близкий к ChatGPT, но заточенный под твой стек задач 🧠\n\n"
        "Основные режимы:\n"
        "• 🧠 AI-Medicine — медицинский ассистент, разбор анализов, подготовка к приёму.\n"
        "• 🤖 Универсальный ассистент — любые вопросы и задачи.\n"
        "• 💬 Личный собеседник — поддержка, разговоры, мозговой штурм.\n"
        "• ✍️ Контент-мейкер — посты, структуры, идеи для Telegram.\n\n"
        "Полезные команды:\n"
        "/start — приветствие и главное меню\n"
        "/mode — сменить режим общения\n"
        "/model — выбрать профиль модели (GPT-4, DeepSeek, mini и т.д.)\n"
        "/med — запустить мастер медицинского запроса по шагам\n"
        "/reset — очистить историю диалога\n"
        "/help — эта справка\n\n"
        "Дальше просто общайся со мной обычными сообщениями — я запоминаю контекст диалога."
    )
    await message.answer(text)


@router.message(Command("mode"))
async def cmd_mode(message: Message) -> None:
    """
    Отдельная команда для смены режима.
    """
    user = message.from_user
    if user is None:
        return

    state = get_state(user.id)
    current_mode = state.mode_key or DEFAULT_MODE_KEY

    kb_modes = _build_modes_keyboard(current_mode=current_mode)
    kb_models = _build_models_keyboard(current_profile=state.model_profile)
    kb_modes.attach(kb_models)

    await message.answer(
        "Выбери режим работы ассистента и при необходимости профиль модели:",
        reply_markup=kb_modes.as_markup(),
    )


@router.message(Command("model"))
async def cmd_model(message: Message) -> None:
    """
    Быстрый выбор только профиля модели.
    """
    user = message.from_user
    if user is None:
        return

    state = get_state(user.id)
    kb = _build_models_keyboard(current_profile=state.model_profile)

    await message.answer(
        "Выбери профиль модели (можно оставить <b>Авто</b> — я сам подберу оптимальный вариант):",
        reply_markup=kb.as_markup(),
    )


@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    """
    Сброс истории диалога и очистка мастера.
    """
    user = message.from_user
    if user is None:
        return

    reset_state(user.id)
    _set_wizard_state(user.id, None)

    await message.answer(
        "История диалога очищена 🧹\n"
        "Можем начать с чистого листа — просто напиши новый запрос или запусти мастер мед.запроса."
    )


@router.message(Command("med"))
async def cmd_med(message: Message) -> None:
    """
    Прямая команда для запуска мастера медицинского запроса.
    """
    await _start_med_wizard(message)


# =========================
# CALLBACK-КНОПКИ
# =========================


@router.callback_query(F.data.startswith("set_mode:"))
async def cb_set_mode(callback: CallbackQuery) -> None:
    """
    Пользователь выбрал другой режим (AI-Medicine / общий / контент и т.д.).
    """
    user = callback.from_user
    if user is None:
        await callback.answer()
        return

    data = callback.data or ""
    _, mode_key = data.split(":", 1)

    if mode_key not in CHAT_MODES:
        await callback.answer("Неизвестный режим 🤔", show_alert=True)
        return

    # При смене режима сбрасываем мастер
    _set_wizard_state(user.id, None)

    state = set_mode(user.id, mode_key)
    current_mode = state.mode_key or DEFAULT_MODE_KEY

    kb_modes = _build_modes_keyboard(current_mode=current_mode)
    kb_models = _build_models_keyboard(current_profile=state.model_profile)
    kb_modes.attach(kb_models)

    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=kb_modes.as_markup())

    mode_cfg = CHAT_MODES[mode_key]
    await callback.answer(f"Режим: {mode_cfg.title}")


@router.callback_query(F.data.startswith("set_model:"))
async def cb_set_model(callback: CallbackQuery) -> None:
    """
    Пользователь выбрал другой профиль модели (GPT-4 / mini / OSS / DeepSeek).
    """
    user = callback.from_user
    if user is None:
        await callback.answer()
        return

    data = callback.data or ""
    _, profile = data.split(":", 1)

    try:
        state = set_model_profile(user.id, profile)
    except ValueError:
        await callback.answer("Неизвестный профиль модели 🤔", show_alert=True)
        return

    kb_modes = _build_modes_keyboard(current_mode=state.mode_key or DEFAULT_MODE_KEY)
    kb_models = _build_models_keyboard(current_profile=state.model_profile)
    kb_modes.attach(kb_models)

    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=kb_modes.as_markup())

    label = get_model_profile_label(state.model_profile)
    await callback.answer(f"Модель: {label}")


@router.callback_query(F.data == "wizard:med")
async def cb_wizard_med(callback: CallbackQuery) -> None:
    """
    Запуск мастера медицинского запроса по кнопке.
    """
    user = callback.from_user
    if user is None:
        await callback.answer()
        return

    if callback.message:
        await _start_med_wizard(callback.message)
    await callback.answer("Мастер медицинского запроса запущен 🩺")


async def _handle_answer_action(callback: CallbackQuery, action: str) -> None:
    """
    Обработка кнопок под ответом: упростить, чек-лист, раскрыть.
    """
    user = callback.from_user
    if user is None:
        await callback.answer()
        return

    user_name = user.first_name or user.username or ""

    if action == "simplify":
        prompt = (
            "Пожалуйста, переформулируй свой предыдущий ответ проще и короче, "
            "как для умного, но уставшего человека. Сохрани суть, убери воду."
        )
    elif action == "checklist":
        prompt = (
            "Преобразуй свой предыдущий ответ в чёткий чек-лист действий. "
            "Каждый пункт с новой строки, максимум 10 пунктов."
        )
    elif action == "expand":
        prompt = (
            "Раскрой свой предыдущий ответ подробнее, но без лишней воды. "
            "Добавь структурированные блоки с подзаголовками и краткими пояснениями."
        )
    else:
        await callback.answer()
        return

    if not callback.message:
        await callback.answer()
        return

    async with ChatActionSender.typing(
        bot=callback.message.bot,
        chat_id=callback.message.chat.id,
    ):
        try:
            answer = await ask_ai(
                user_id=user.id,
                text=prompt,
                user_name=user_name,
            )
        except RateLimitError as e:
            if e.scope == "minute":
                await callback.message.answer(
                    "Слишком много запросов за последнюю минуту 🧨\n"
                    "Попробуй ещё раз через 20–30 секунд."
                )
            else:
                await callback.message.answer(
                    "Достигнут дневной лимит запросов для этого бота 🚫\n"
                    "Лимит обновится завтра."
                )
            await callback.answer()
            return
        except Exception:
            logger.exception("Error in answer action")
            await callback.message.answer(
                "Не получилось обработать запрос к модели для этого ответа 😔\n"
                "Попробуй ещё раз."
            )
            await callback.answer()
            return

    kb_actions = _build_answer_actions_keyboard()
    chunks = _split_text(answer)
    if not chunks:
        await callback.answer()
        return

    # Отправляем новый блок с теми же кнопками
    await callback.message.answer(chunks[0], reply_markup=kb_actions.as_markup())
    for chunk in chunks[1:]:
        await callback.message.answer(chunk)

    await callback.answer()


@router.callback_query(F.data == "ans:simplify")
async def cb_ans_simplify(callback: CallbackQuery) -> None:
    await _handle_answer_action(callback, "simplify")


@router.callback_query(F.data == "ans:checklist")
async def cb_ans_checklist(callback: CallbackQuery) -> None:
    await _handle_answer_action(callback, "checklist")


@router.callback_query(F.data == "ans:expand")
async def cb_ans_expand(callback: CallbackQuery) -> None:
    await _handle_answer_action(callback, "expand")


# =========================
# ОСНОВНОЙ ОБРАБОТЧИК СООБЩЕНИЙ
# =========================


@router.message(F.text & ~F.via_bot)
async def handle_chat(message: Message) -> None:
    """
    Главный обработчик текста:
    - если активен мастер медицинского запроса, продолжаем шаги мастера;
    - иначе — обычное обращение к ИИ с учётом режима/модели.
    """
    user = message.from_user
    if user is None:
        return

    user_id = user.id
    user_name = user.first_name or user.username or ""

    # Если активен мастер мед.запроса — обрабатываем шаги и не идём в обычный чат
    wizard = _get_wizard_state(user_id)
    if wizard and wizard.get("type") == "med":
        await _process_med_wizard(message, wizard)
        return

    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        try:
            answer = await ask_ai(
                user_id=user_id,
                text=message.text or "",
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

    kb_actions = _build_answer_actions_keyboard()

    chunks = _split_text(answer)
    if not chunks:
        return

    # К первому блоку цепляем кнопки управления
    await message.answer(chunks[0], reply_markup=kb_actions.as_markup())
    for chunk in chunks[1:]:
        await message.answer(chunk)
