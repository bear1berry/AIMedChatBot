from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot import config as bot_config
from services.llm import Answer, generate_answer, DEFAULT_MODE_KEY

# ==============================
#   Логирование
# ==============================

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ==============================
#   Сессии пользователя и история
# ==============================


@dataclass
class UserSession:
    user_id: int
    active_mode: str = DEFAULT_MODE_KEY
    history: List[Dict[str, str]] = field(default_factory=list)


USER_SESSIONS: Dict[int, UserSession] = {}

# Последний ответ для "✏️ Продолжить"
LAST_ANSWERS: Dict[int, Answer] = {}
# Запросы на "🔍 Раскрыть подробнее"
EXPAND_REQUESTS: Dict[int, Dict[str, Any]] = {}


def get_session(user_id: int) -> UserSession:
    session = USER_SESSIONS.get(user_id)
    if session is None:
        session = UserSession(user_id=user_id, active_mode=DEFAULT_MODE_KEY)
        USER_SESSIONS[user_id] = session
    return session


def update_history(
    session: UserSession,
    user_prompt: str,
    assistant_text: str,
    max_turns: int = 8,
) -> None:
    """Добавляем в историю пользователю пару user/assistant и подрезаем до последних N оборотов."""
    session.history.append({"role": "user", "content": user_prompt})
    session.history.append({"role": "assistant", "content": assistant_text})

    max_len = max_turns * 2
    if len(session.history) > max_len:
        session.history = session.history[-max_len:]


# ==============================
#   Клавиатуры (таскбар)
# ==============================


def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton(text="🧠 Режимы", callback_data="menu:modes"),
            InlineKeyboardButton(text="👤 Профиль", callback_data="menu:profile"),
        ],
        [
            InlineKeyboardButton(text="💎 Подписка", callback_data="menu:subscription"),
            InlineKeyboardButton(text="👥 Рефералы", callback_data="menu:referrals"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def build_modes_keyboard(active_mode: str) -> InlineKeyboardMarkup:
    def mode_button(label: str, key: str) -> InlineKeyboardButton:
        prefix = "✅ " if key == active_mode else ""
        return InlineKeyboardButton(text=prefix + label, callback_data=f"mode:{key}")

    rows = [
        [
            mode_button("🧠 Универсальный", "universal"),
            mode_button("🩺 Медицина", "medical"),
        ],
        [
            mode_button("🔥 Наставник", "mentor"),
            mode_button("💼 Бизнес", "business"),
        ],
        [
            mode_button("🎨 Креатив", "creative"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:root"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ==============================
#   Живое печатание 2.0
# ==============================


async def stream_answer(
    message: Message,
    mode_key: str,
    user_text: str,
    history: Optional[List[Dict[str, str]]] = None,
    style_hint: Optional[str] = None,
    force_mode: Optional[str] = None,  # "quick" | "deep"
    edit_message: Optional[Message] = None,
) -> Answer:
    """
    Рендерит ответ с "живым печатанием" и возвращает Answer (для истории).

    - Если edit_message не задан → создаёт новое сообщение "…" и редактирует его.
    - Если передан edit_message (например, из callback) → перезаписывает его содержимое.
    """
    chat_id = message.chat.id

    if edit_message is None:
        msg = await message.answer("…")
    else:
        msg = edit_message

    msg_id = msg.message_id

    # Вызываем LLM-ядро
    answer = await generate_answer(
        mode_key=mode_key,
        user_prompt=user_text,
        history=history,
        style_hint=style_hint,
        force_mode=force_mode,
    )

    # Сохраняем последний ответ для "✏️ Продолжить"
    LAST_ANSWERS[chat_id] = answer

    text_acc = ""
    keyboard: Optional[InlineKeyboardMarkup] = None

    for idx, ch in enumerate(answer.chunks):
        sep = "\n\n" if text_acc else ""
        text_acc += sep + ch.text

        text_to_show = text_acc
        keyboard = None
        is_last = idx == len(answer.chunks) - 1

        if is_last:
            buttons = []

            # Короткий ответ → можно раскрыть
            if answer.meta.get("can_expand") and answer.meta.get("answer_mode") == "quick":
                buttons.append(
                    [
                        InlineKeyboardButton(
                            text="🔍 Раскрыть подробнее",
                            callback_data="expand_answer",
                        )
                    ]
                )
                EXPAND_REQUESTS[chat_id] = {
                    "mode_key": mode_key,
                    "user_text": user_text,
                    "style_hint": style_hint,
                }

            # Ответ обрезан по длине → добавляем текстовый триггер
            if answer.has_more:
                text_to_show = text_acc + "\n\n✏️ Продолжить"

            if buttons:
                keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        try:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text_to_show,
                reply_markup=keyboard,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to edit message: %s", e)

        await asyncio.sleep(0.03 if answer.meta.get("answer_mode") == "quick" else 0.06)

    # На всякий случай, если чанков нет
    if not answer.chunks:
        try:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=answer.full_text or "Что-то пошло не так, попробуй ещё раз.",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to edit message (empty answer): %s", e)

    return answer


# ==============================
#   Router и хендлеры
# ==============================

router = Router()


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    user_id = message.from_user.id
    session = get_session(user_id)
    session.active_mode = DEFAULT_MODE_KEY
    session.history.clear()

    text = (
        "Привет! Я BlackBox GPT — твой универсальный ИИ-ассистент.\n\n"
        "Минимализм, максимум мозга. Пиши любой запрос — от медицины до бизнеса и "
        "личного развития.\n\n"
        "Выбери режим в нижнем меню или просто задай вопрос."
    )
    await message.answer(text, reply_markup=build_main_menu_keyboard())


@router.callback_query(F.data.startswith("menu:"))
async def on_menu_callback(cb: CallbackQuery) -> None:
    if cb.message is None:
        await cb.answer()
        return

    data = cb.data or ""
    chat_id = cb.message.chat.id
    user_id = cb.from_user.id
    session = get_session(user_id)

    if data == "menu:root":
        await cb.answer()
        await cb.message.edit_text(
            "Главное меню. Выбери, что дальше:",
            reply_markup=build_main_menu_keyboard(),
        )
        return

    if data == "menu:modes":
        await cb.answer()
        await cb.message.edit_text(
            "Выбери режим работы ассистента:",
            reply_markup=build_modes_keyboard(session.active_mode),
        )
        return

    if data == "menu:profile":
        await cb.answer("Профиль скоро прокачаем ещё сильнее ⚙️", show_alert=False)
        await cb.message.edit_text(
            "Профиль пользователя.\n"
            f"Текущий режим: <b>{session.active_mode}</b>\n\n"
            "Здесь в будущем будут храниться твои предпочтения и настройки.",
            reply_markup=build_main_menu_keyboard(),
            parse_mode="HTML",
        )
        return

    if data == "menu:subscription":
        await cb.answer("Подписка в разработке 💎", show_alert=False)
        await cb.message.edit_text(
            "Подписка BlackBox GPT.\n\n"
            "В будущем здесь появится Premium-доступ к более мощным моделям, "
            "расширенная память и дополнительные режимы.",
            reply_markup=build_main_menu_keyboard(),
        )
        return

    if data == "menu:referrals":
        await cb.answer("Реферальная система появится позже 👥", show_alert=False)
        await cb.message.edit_text(
            "Реферальная программа скоро появится.\n\n"
            "Ты сможешь приглашать людей и получать бонусы.",
            reply_markup=build_main_menu_keyboard(),
        )
        return

    await cb.answer()
    logger.info("Unknown menu callback from chat %s: %s", chat_id, data)


@router.callback_query(F.data.startswith("mode:"))
async def on_mode_change(cb: CallbackQuery) -> None:
    if cb.message is None:
        await cb.answer()
        return

    data = cb.data or ""
    parts = data.split(":", 1)
    if len(parts) != 2:
        await cb.answer()
        return

    mode_key = parts[1]
    user_id = cb.from_user.id
    session = get_session(user_id)
    session.active_mode = mode_key

    await cb.answer("Режим обновлён ✅", show_alert=False)

    await cb.message.edit_text(
        "Режим обновлён.\n\n"
        "🧠 Сейчас ты в режиме: "
        f"<b>{mode_key}</b>\n\n"
        "Можешь сразу писать запрос или выбрать другой режим.",
        reply_markup=build_modes_keyboard(session.active_mode),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "expand_answer")
async def on_expand_answer(cb: CallbackQuery) -> None:
    message = cb.message
    if message is None:
        await cb.answer()
        return

    chat_id = message.chat.id
    user_id = cb.from_user.id
    session = get_session(user_id)

    params = EXPAND_REQUESTS.get(chat_id)
    if not params:
        await cb.answer("Не нашёл, что раскрывать 🙃", show_alert=False)
        return

    await cb.answer()

    mode_key = params.get("mode_key", session.active_mode)
    user_text = params.get("user_text", "")
    style_hint = params.get("style_hint")

    # Глубокий разбор того же запроса, в том же контексте
    answer = await stream_answer(
        message=message,
        mode_key=mode_key,
        user_text=user_text,
        history=session.history,
        style_hint=style_hint,
        force_mode="deep",
        edit_message=message,
    )

    assistant_text = answer.meta.get("full_text") or answer.full_text
    update_history(
        session,
        user_prompt="Раскрой подробнее предыдущий ответ.",
        assistant_text=assistant_text,
    )


@router.message(F.text.regexp(r"(?i)^\s*продолж(и|ить)\s*$"))
async def on_continue_request(message: Message) -> None:
    chat_id = message.chat.id
    user_id = message.from_user.id
    session = get_session(user_id)

    last = LAST_ANSWERS.get(chat_id)
    if not last or not last.meta.get("truncated"):
        await message.answer("Предыдущий ответ уже полный. Пиши новый запрос 🙂")
        return

    # Продолжаем предыдущую мысль, опираясь на текущую историю
    answer = await stream_answer(
        message=message,
        mode_key=session.active_mode,
        user_text="Продолжи, пожалуйста, предыдущий ответ.",
        history=session.history,
        style_hint=None,
    )

    assistant_text = answer.meta.get("full_text") or answer.full_text
    update_history(
        session,
        user_prompt="Продолжи, пожалуйста, предыдущий ответ.",
        assistant_text=assistant_text,
    )


@router.message(F.text)
async def on_user_message(message: Message) -> None:
    """
    Главный диалоговый хендлер:
    - учитывает текущий режим (универсальный / мед / бизнес / наставник / креатив),
    - пробрасывает историю в ядро,
    - запускает "живое печатание".
    """
    # Игнорируем команды, их перехватывают отдельные хендлеры
    if message.text.startswith("/"):
        return

    user_id = message.from_user.id
    session = get_session(user_id)

    user_text = message.text.strip()
    if not user_text:
        await message.answer("Напиши что-нибудь содержательное 🙂")
        return

    # История диалога и режим уже в session
    answer = await stream_answer(
        message=message,
        mode_key=session.active_mode,
        user_text=user_text,
        history=session.history,
        style_hint=None,
    )

    assistant_text = answer.meta.get("full_text") or answer.full_text
    update_history(
        session,
        user_prompt=user_text,
        assistant_text=assistant_text,
    )


# ==============================
#   Запуск бота
# ==============================

async def main() -> None:
    bot = Bot(token=bot_config.BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    logger.info("Starting polling for bot...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
