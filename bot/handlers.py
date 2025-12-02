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
    list_workspaces,
    get_current_workspace,
    set_current_workspace,
    create_workspace,
)
from .modes import CHAT_MODES, DEFAULT_MODE_KEY, get_mode_label, list_modes_for_menu

logger = logging.getLogger(__name__)

router = Router()


# =========================
# КЛАВИАТУРЫ
# =========================


def build_main_reply_keyboard() -> ReplyKeyboardMarkup:
    """
    Нижняя клавиатура (как системные кнопки Telegram):
    🧠 Пространства | ⚙️ Настройки
    🆘 Помощь       | 🔁 Перезапуск
    """
    keyboard = [
        [
            KeyboardButton(text="🧠 Пространства"),
            KeyboardButton(text="⚙️ Настройки"),
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
    # Логичный порядок: универсальный → собеседник → контент → здоровье
    order = [
        "chatgpt_general",
        "friendly_chat",
        "content_creator",
        "ai_medicine_assistant",
    ]
    for key in order:
        mode = CHAT_MODES.get(key)
        if not mode:
            continue
        mark = "✅" if key == current_mode else "⚪️"
        kb.button(text=f"{mark} {mode.title}", callback_data=f"set_mode:{key}")
    kb.adjust(1)
    return kb


def _build_models_keyboard(current_profile: str) -> InlineKeyboardBuilder:
    """
    Кнопки выбора профиля модели.
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


def _build_workspaces_keyboard(user_id: int) -> InlineKeyboardBuilder:
    """
    Инлайн-меню выбора workspace.
    """
    state = get_state(user_id)
    current_id = state.current_workspace_id
    workspaces = list_workspaces(user_id)

    kb = InlineKeyboardBuilder()
    for ws in workspaces:
        mark = "✅" if ws.id == current_id else "⚪️"
        kb.button(text=f"{mark} {ws.title}", callback_data=f"ws:switch:{ws.id}")

    kb.adjust(1)
    kb.button(text="➕ Новое пространство", callback_data="ws:new")
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
# КОМАНДЫ
# =========================


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """
    Стартовый экран: чистый текст + нижняя клавиатура.
    """
    user = message.from_user
    if user is None:
        return

    state = get_state(user.id)
    current_ws = get_current_workspace(user.id)
    current_mode = state.mode_key or DEFAULT_MODE_KEY
    current_mode_label = get_mode_label(current_mode)
    current_profile_label = get_model_profile_label(state.model_profile)

    text = (
        f"Привет, {user.first_name or 'друг'}! 👋\n\n"
        "<b>AIMed</b> — твой персональный ИИ-центр.\n\n"
        "Я помогу с задачами по работе, учёбе, личной жизни и проектам — "
        "в одном месте, но с разными пространствами.\n\n"
        "<b>Текущее пространство:</b>\n"
        f"• 🧠 <b>{current_ws.title}</b>\n\n"
        "<b>Сейчас выбрано:</b>\n"
        f"• Режим: <b>{current_mode_label}</b>\n"
        f"• Модель: <b>{current_profile_label}</b>\n\n"
        "Используй кнопки внизу или просто напиши свой запрос."
    )

    await message.answer(text, reply_markup=build_main_reply_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "Я универсальный ИИ-ассистент с концепцией рабочих пространств (workspaces).\n\n"
        "Каждое пространство — это отдельный контекст диалога, режим и история:\n"
        "• можно держать отдельно канал, учёбу, работу и личное;\n"
        "• переключаться между ними через кнопку <b>🧠 Пространства</b>.\n\n"
        "Режимы общения:\n"
        "• 🤖 Универсальный ассистент — любые вопросы и задачи;\n"
        "• 💬 Личный собеседник — поддержка, рефлексия, мозговой штурм;\n"
        "• ✍️ Контент-мейкер — посты, карусели, сценарии;\n"
        "• ⚕️ Здоровье и медицина — общая справочная информация (без диагноза и назначений).\n\n"
        "Кнопки внизу:\n"
        "• 🧠 Пространства — выбор/создание workspace;\n"
        "• ⚙️ Настройки — выбор режима и модели для текущего пространства;\n"
        "• 🆘 Помощь — это сообщение;\n"
        "• 🔁 Перезапуск — очистить историю текущего пространства.\n\n"
        "Команды: /start, /help, /mode, /model, /reset."
    )
    await message.answer(text)


@router.message(Command("mode"))
async def cmd_mode(message: Message) -> None:
    """
    Быстрый выбор режима общения для текущего workspace.
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
        "Выбери режим и профиль модели для <b>текущего</b> пространства:",
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
    Очистка истории только текущего workspace.
    """
    user = message.from_user
    if user is None:
        return

    reset_state(user.id)
    current_ws = get_current_workspace(user.id)
    await message.answer(
        f"История пространства <b>{current_ws.title}</b> очищена 🧹\n"
        "Можем начать с чистого листа — просто напиши новый запрос.",
        reply_markup=build_main_reply_keyboard(),
    )


# =========================
# НИЖНЯЯ КЛАВИАТУРА
# =========================


@router.message(F.text == "🧠 Пространства")
async def on_btn_workspaces(message: Message) -> None:
    """
    Открываем меню рабочих пространств.
    """
    user = message.from_user
    if user is None:
        return

    kb = _build_workspaces_keyboard(user.id)
    current_ws = get_current_workspace(user.id)

    text = (
        "🧠 <b>Твои пространства</b>\n\n"
        "Каждое пространство — отдельный контекст диалога.\n"
        "Сейчас активно: "
        f"<b>{current_ws.title}</b>.\n\n"
        "Нажми на нужное пространство, чтобы переключиться, или создай новое."
    )

    await message.answer(text, reply_markup=kb.as_markup())


@router.message(F.text == "⚙️ Настройки")
async def on_btn_settings(message: Message) -> None:
    """
    Открываем экран настроек: режим + модель.
    """
    user = message.from_user
    if user is None:
        return

    state = get_state(user.id)
    current_ws = get_current_workspace(user.id)
    current_mode = state.mode_key or DEFAULT_MODE_KEY

    kb_modes = _build_modes_keyboard(current_mode=current_mode)
    kb_models = _build_models_keyboard(current_profile=state.model_profile)
    kb_modes.attach(kb_models)

    text = (
        "⚙️ <b>Настройки для текущего пространства</b>\n\n"
        f"Пространство: <b>{current_ws.title}</b>\n\n"
        "Выбери режим общения и профиль модели:"
    )
    await message.answer(text, reply_markup=kb_modes.as_markup())


@router.message(F.text == "🆘 Помощь")
async def on_btn_help(message: Message) -> None:
    await cmd_help(message)


@router.message(F.text == "🔁 Перезапуск")
async def on_btn_restart(message: Message) -> None:
    user = message.from_user
    if user is None:
        return

    reset_state(user.id)
    await cmd_start(message)


# =========================
# CALLBACK-КНОПКИ
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


@router.callback_query(F.data.startswith("ws:switch:"))
async def callback_ws_switch(callback: CallbackQuery) -> None:
    """
    Переключение между workspace.
    """
    data = callback.data or ""
    parts = data.split(":", 2)
    if len(parts) != 3:
        await callback.answer()
        return

    user = callback.from_user
    if user is None:
        await callback.answer()
        return

    ws_id = parts[2]
    ws = set_current_workspace(user.id, ws_id)

    kb = _build_workspaces_keyboard(user.id)

    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=kb.as_markup())

    await callback.answer(f"Активно пространство: {ws.title}")


@router.callback_query(F.data == "ws:new")
async def callback_ws_new(callback: CallbackQuery) -> None:
    """
    Создать новое пространство с дефолтным названием.
    """
    user = callback.from_user
    if user is None:
        await callback.answer()
        return

    ws = create_workspace(user.id, title="")
    kb = _build_workspaces_keyboard(user.id)

    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=kb.as_markup())

    await callback.answer(f"Создано и выбрано: {ws.title}")


# =========================
# ОСНОВНОЙ ОБРАБОТЧИК СООБЩЕНИЙ
# =========================


@router.message(F.text & ~F.via_bot)
async def handle_chat(message: Message) -> None:
    """
    Всё, что не совпало с кнопками и командами, идёт как обычный запрос к ИИ
    в контексте текущего workspace.
    """
    user = message.from_user
    if user is None:
        return

    user_id = user.id
    user_name = user.first_name or user.username or ""
    current_ws = get_current_workspace(user_id)

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

    # Добавляем лейбл workspace перед ответом
    header = f"🧠 Workspace: <b>{current_ws.title}</b>\n\n"
    full = header + (answer or "")
    for chunk in _split_text(full):
        await message.answer(chunk)
