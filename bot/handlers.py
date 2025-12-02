from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
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
# КЛАВИАТУРЫ
# =========================


def build_main_reply_keyboard() -> ReplyKeyboardMarkup:
    """
    Главная клавиатура внизу экрана (как на скриншоте):
    🎓 Для учёбы | ⚙️ Настройки бота
    🆘 Помощь    | 🔁 Перезапуск
    """
    keyboard = [
        [
            KeyboardButton(text="🎓 Для учёбы"),
            KeyboardButton(text="⚙️ Настройки бота"),
        ],
        [
            KeyboardButton(text="🆘 Помощь"),
            KeyboardButton(text="🔁 Перезапуск"),
        ],
    ]
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _build_modes_keyboard(current_mode: str) -> InlineKeyboardBuilder:
    """
    Кнопки выбора режима ассистента (используются только в разделе настроек).
    """
    kb = InlineKeyboardBuilder()
    for key, label in list_modes_for_menu().items():
        mark = "✅" if key == current_mode else "⚪️"
        kb.button(text=f"{mark} {label}", callback_data=f"set_mode:{key}")
    kb.adjust(1)
    return kb


def _build_models_keyboard(current_profile: str) -> InlineKeyboardBuilder:
    """
    Кнопки выбора профиля модели (авто, GPT-4.1, DeepSeek и т.д.).
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
    Минималистичный экран приветствия без инлайн-кнопок —
    всё управление уходит в нижнюю клавиатуру.
    """
    user = message.from_user
    if user is None:
        return

    state = get_state(user.id)
    current_mode = state.mode_key or DEFAULT_MODE_KEY
    current_mode_label = get_mode_label(current_mode)
    current_profile_label = get_model_profile_label(state.model_profile)

    text = (
        f"Привет, {user.first_name or 'друг'}! 👋\n\n"
        "<b>AIMed</b> — твой персональный ИИ-ассистент.\n\n"
        "Что я умею:\n"
        "• 🤖 Помогать с любыми вопросами и задачами.\n"
        "• 🎓 Разобраться в учёбе и сложных темах.\n"
        "• ✍️ Подбирать формулировки, улучшать тексты и генерировать идеи.\n"
        "• ⚕️ Давать общую справочную информацию по здоровью (без диагноза и назначений).\n\n"
        "<b>Сейчас выбрано:</b>\n"
        f"• Режим: <b>{current_mode_label}</b>\n"
        f"• Модель: <b>{current_profile_label}</b>\n\n"
        "Используй кнопки внизу или просто напиши свой запрос."
    )

    await message.answer(text, reply_markup=build_main_reply_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "Я универсальный ИИ-ассистент с минималистичным интерфейсом.\n\n"
        "Режимы работы:\n"
        "• 🤖 Универсальный ассистент — любые вопросы и задачи.\n"
        "• 💬 Личный собеседник — поддержка, идеи, рефлексия.\n"
        "• ✍️ Контент-мейкер — посты, карусели, сценарии.\n"
        "• 🧠 AI-Medicine — справочная информация по здоровью (без диагноза и назначений).\n\n"
        "Кнопки внизу:\n"
        "• 🎓 Для учёбы — акцент на объяснения и обучение.\n"
        "• ⚙️ Настройки бота — выбор режима и модели.\n"
        "• 🆘 Помощь — эта шпаргалка.\n"
        "• 🔁 Перезапуск — очистка диалога и возврат к стартовому экрану.\n\n"
        "Команды тоже доступны: /start, /mode, /model, /reset."
    )
    await message.answer(text)


@router.message(Command("mode"))
async def cmd_mode(message: Message) -> None:
    """
    Отдельная команда для смены режима (через инлайн-клавиатуру).
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
        "Выбери режим и профиль модели:",
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
        "Выбери профиль модели (можно оставить 🤖 Авто — я сам подберу оптимальный вариант):",
        reply_markup=kb.as_markup(),
    )


@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    """
    Полный сброс диалога.
    """
    user = message.from_user
    if user is None:
        return

    reset_state(user.id)
    await message.answer(
        "История диалога очищена 🧹\n"
        "Можем начать с чистого листа — напиши новый запрос или используй кнопки внизу.",
        reply_markup=build_main_reply_keyboard(),
    )


# =========================
# ОБРАБОТЧИКИ НИЖНЕЙ КЛАВИАТУРЫ
# =========================


@router.message(F.text == "🎓 Для учёбы")
async def on_btn_study(message: Message) -> None:
    """
    Переключаем акцент на обучение.
    Сейчас это мапится на универсальный режим; при желании можно завести отдельный.
    """
    user = message.from_user
    if user is None:
        return

    # Если появится отдельный учебный режим — укажи здесь его ключ вместо chatgpt_general
    set_mode(user.id, "chatgpt_general")

    await message.answer(
        "Ок, делаем фокус на учёбе. Задавай вопросы по предметам, теориям, экзаменам — разберём по полочкам. 📚",
        reply_markup=build_main_reply_keyboard(),
    )


@router.message(F.text == "⚙️ Настройки бота")
async def on_btn_settings(message: Message) -> None:
    """
    Открываем экран настроек: выбор режима + модели через инлайн-клавиатуру.
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
        "Настройки бота. Выбери режим и профиль модели:",
        reply_markup=kb_modes.as_markup(),
    )


@router.message(F.text == "🆘 Помощь")
async def on_btn_help(message: Message) -> None:
    """
    Просто переиспользуем /help.
    """
    await cmd_help(message)


@router.message(F.text == "🔁 Перезапуск")
async def on_btn_restart(message: Message) -> None:
    """
    Очистка истории и возврат к стартовому экрану.
    """
    user = message.from_user
    if user is None:
        return

    reset_state(user.id)
    await cmd_start(message)


# =========================
# CALLBACK-КНОПКИ НАСТРОЕК
# =========================


@router.callback_query(F.data.startswith("set_mode:"))
async def callback_set_mode(callback: CallbackQuery) -> None:
    if not callback.data:
        await callback.answer()
        return

    user = callback.from_user
    if user is None:
        await callback.answer()
        return

    mode_key = callback.data.split(":", 1)[1]
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

    mode_label = get_mode_label(current_mode)
    await callback.answer(f"Режим: {mode_label}")


@router.callback_query(F.data.startswith("set_model:"))
async def callback_set_model(callback: CallbackQuery) -> None:
    if not callback.data:
        await callback.answer()
        return

    user = callback.from_user
    if user is None:
        await callback.answer()
        return

    profile = callback.data.split(":", 1)[1]
    try:
        state = set_model_profile(user.id, profile)
    except ValueError:
        await callback.answer("Неизвестный профиль модели 🤔", show_alert=True)
        return

    current_mode = state.mode_key or DEFAULT_MODE_KEY
    kb_modes = _build_modes_keyboard(current_mode=current_mode)
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
    Всё, что не совпало с кнопками, идёт как обычный запрос к ИИ.
    """
    user = message.from_user
    if user is None:
        return

    user_id = user.id
    user_name = user.first_name or user.username or ""

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

    for chunk in _split_text(answer):
        await message.answer(chunk)
