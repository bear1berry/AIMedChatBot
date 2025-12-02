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
    Кнопки выбора режима ассистента (медицинский, универсальный, беседа, контент).
    Порядок берём из CHAT_MODES через list_modes_for_menu().
    """
    kb = InlineKeyboardBuilder()
    for key, label in list_modes_for_menu().items():
        mark = "✅" if key == current_mode else "⚪️"
        kb.button(text=f"{mark} {label}", callback_data=f"set_mode:{key}")
    kb.adjust(1)
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


# =========================
# КОМАНДЫ
# =========================

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """
    Приветствие + главное «джобсовское» меню: режимы + профиль модели.
    """
    user = message.from_user
    assert user is not None

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
        "При необходимости режим и модель можно сменить кнопками ниже 👇"
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
        "Команды:\n"
        "/start — приветствие и главное меню\n"
        "/mode — быстро сменить режим общения\n"
        "/model — выбрать профиль модели (GPT-4, mini, DeepSeek и т.д.)\n"
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
    assert user is not None
    state = get_state(user.id)

    kb_modes = _build_modes_keyboard(current_mode=state.mode_key or DEFAULT_MODE_KEY)
    kb_models = _build_models_keyboard(current_profile=state.model_profile)
    kb_modes.attach(kb_models)

    await message.answer(
        "Выбери режим работы ассистента и, при желании, профиль модели:",
        reply_markup=kb_modes.as_markup(),
    )


@router.message(Command("model"))
async def cmd_model(message: Message) -> None:
    """
    Быстрый выбор только профиля модели.
    """
    user = message.from_user
    assert user is not None
    state = get_state(user.id)

    kb = _build_models_keyboard(current_profile=state.model_profile)

    await message.answer(
        "Выбери профиль модели (можно оставить <b>Авто</b> — я сам подберу оптимальный вариант):",
        reply_markup=kb.as_markup(),
    )


@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    """
    Сброс истории диалога, но без смены режима/модели.
    """
    user = message.from_user
    assert user is not None

    reset_state(user.id)
    await message.answer(
        "История диалога очищена 🧹\n"
        "Можем начать с чистого листа — просто напиши новый запрос."
    )


# =========================
# CALLBACK-КНОПКИ
# =========================

@router.callback_query(F.data.startswith("set_mode:"))
async def cb_set_mode(callback: CallbackQuery) -> None:
    """
    Пользователь выбрал другой режим (медицинский / общий / контент и т.д.).
    """
    user = callback.from_user
    assert user is not None

    data = callback.data or ""
    _, mode_key = data.split(":", 1)

    if mode_key not in CHAT_MODES:
        await callback.answer("Неизвестный режим 🤔", show_alert=True)
        return

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
    assert user is not None

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


# =========================
# ОСНОВНОЙ ОБРАБОТЧИК СООБЩЕНИЙ
# =========================

@router.message(F.text & ~F.via_bot)
async def handle_chat(message: Message) -> None:
    """
    Главный обработчик текста: прогоняем запрос через ask_ai с учётом режима/модели.
    """
    user = message.from_user
    if user is None:
        return

    user_name = user.first_name or user.username or ""

    # красивый индикатор «печатает…»
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
