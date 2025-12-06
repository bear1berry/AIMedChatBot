from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
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
#   Константы интерфейса
# ==============================

BTN_MODES = "🧠 Режимы"
BTN_PROFILE = "👤 Профиль"
BTN_SUBSCRIPTION = "💎 Подписка"
BTN_REFERRALS = "👥 Рефералы"

BTN_MODE_UNIVERSAL = "🧠 Универсальный"
BTN_MODE_MEDICAL = "🩺 Медицина"
BTN_MODE_MENTOR = "🔥 Наставник"
BTN_MODE_BUSINESS = "💼 Бизнес"
BTN_MODE_CREATIVE = "🎨 Креатив"
BTN_BACK = "⬅️ Назад"

MENU_BUTTON_TEXTS = {
    BTN_MODES,
    BTN_PROFILE,
    BTN_SUBSCRIPTION,
    BTN_REFERRALS,
    BTN_MODE_UNIVERSAL,
    BTN_MODE_MEDICAL,
    BTN_MODE_MENTOR,
    BTN_MODE_BUSINESS,
    BTN_MODE_CREATIVE,
    BTN_BACK,
}

TEXT_TO_MODE_KEY: Dict[str, str] = {
    BTN_MODE_UNIVERSAL: "universal",
    BTN_MODE_MEDICAL: "medical",
    BTN_MODE_MENTOR: "mentor",
    BTN_MODE_BUSINESS: "business",
    BTN_MODE_CREATIVE: "creative",
}

UI_ROOT = "root"
UI_MODES = "modes"

# ==============================
#   Сессии пользователя
# ==============================


@dataclass
class UserSession:
    user_id: int
    active_mode: str = DEFAULT_MODE_KEY
    ui_screen: str = UI_ROOT
    history: List[Dict[str, str]] = field(default_factory=list)


USER_SESSIONS: Dict[int, UserSession] = {}

# Для "✏️ Продолжить"
LAST_ANSWERS: Dict[int, Answer] = {}


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
    session.history.append({"role": "user", "content": user_prompt})
    session.history.append({"role": "assistant", "content": assistant_text})

    max_len = max_turns * 2
    if len(session.history) > max_len:
        session.history = session.history[-max_len:]


# ==============================
#   Клавиатуры (таскбар)
# ==============================


def build_root_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_MODES), KeyboardButton(text=BTN_PROFILE)],
            [KeyboardButton(text=BTN_SUBSCRIPTION), KeyboardButton(text=BTN_REFERRALS)],
        ],
        resize_keyboard=True,
    )


def build_modes_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_MODE_UNIVERSAL), KeyboardButton(text=BTN_MODE_MEDICAL)],
            [KeyboardButton(text=BTN_MODE_MENTOR), KeyboardButton(text=BTN_MODE_BUSINESS)],
            [KeyboardButton(text=BTN_MODE_CREATIVE), KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True,
    )


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
) -> Answer:
    """
    Рендерит ответ "живым печатанием" и возвращает Answer для истории.
    Только текст, никаких inline-кнопок.
    """
    chat_id = message.chat.id

    draft = await message.answer("…", reply_markup=build_root_keyboard())
    msg_id = draft.message_id

    answer = await generate_answer(
        mode_key=mode_key,
        user_prompt=user_text,
        history=history,
        style_hint=style_hint,
        force_mode=force_mode,
    )

    LAST_ANSWERS[chat_id] = answer

    text_acc = ""

    for ch in answer.chunks:
        sep = "\n\n" if text_acc else ""
        text_acc += sep + ch.text

        text_to_show = text_acc
        if answer.has_more:
            # мягкая подсказка, без кнопок
            text_to_show = text_acc + "\n\n✏️ Чтобы продолжить, напиши: продолжи"

        try:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text_to_show,
                reply_markup=build_root_keyboard(),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to edit message: %s", e)

        await asyncio.sleep(0.03 if answer.meta.get("answer_mode") == "quick" else 0.06)

    if not answer.chunks:
        try:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=answer.full_text or "Что-то пошло не так, попробуй ещё раз.",
                reply_markup=build_root_keyboard(),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to edit empty answer: %s", e)

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
    session.ui_screen = UI_ROOT
    session.history.clear()

    text = (
        "Привет. Я BlackBox GPT — универсальный ИИ-ассистент премиум-класса.\n\n"
        "Минимализм во всём: только диалог и нижний таскбар. "
        "Пиши любой запрос — от медицины и бизнеса до личного развития.\n\n"
        "Выбери режим в таскбаре или просто задай вопрос."
    )
    await message.answer(text, reply_markup=build_root_keyboard())


# ---------- Навигация таскбара ----------

@router.message(F.text == BTN_MODES)
async def on_modes_menu(message: Message) -> None:
    session = get_session(message.from_user.id)
    session.ui_screen = UI_MODES

    text = (
        "Режимы работы BlackBox GPT.\n\n"
        "Выбери, в каком фокусе сейчас нужен ассистент: универсальный, "
        "медицинский, наставнический, бизнес или креатив."
    )
    await message.answer(text, reply_markup=build_modes_keyboard())


@router.message(F.text == BTN_BACK)
async def on_back(message: Message) -> None:
    session = get_session(message.from_user.id)
    session.ui_screen = UI_ROOT

    text = "Возвращаю нижний таскбар в основной режим."
    await message.answer(text, reply_markup=build_root_keyboard())


@router.message(F.text == BTN_PROFILE)
async def on_profile(message: Message) -> None:
    session = get_session(message.from_user.id)

    text = (
        "Профиль BlackBox GPT.\n\n"
        f"Текущий режим: {session.active_mode}\n"
        "Диалоговая память активна: да (бот учитывает контекст последних сообщений).\n\n"
        "Позже здесь появятся более тонкие настройки под твой стиль общения."
    )
    await message.answer(text, reply_markup=build_root_keyboard())


@router.message(F.text == BTN_SUBSCRIPTION)
async def on_subscription(message: Message) -> None:
    text = (
        "Подписка BlackBox GPT.\n\n"
        "В планах: доступ к более мощным моделям, длинной памяти, "
        "приоритетной обработке и дополнительным режимам.\n\n"
        "Сейчас раздел в разработке, но место под премиум уже зарезервировано."
    )
    await message.answer(text, reply_markup=build_root_keyboard())


@router.message(F.text == BTN_REFERRALS)
async def on_referrals(message: Message) -> None:
    text = (
        "Реферальная система BlackBox GPT.\n\n"
        "В будущем ты сможешь делиться ботом и получать бонусы за приглашённых "
        "пользователей. Позже сюда прилетит твоя персональная ссылка."
    )
    await message.answer(text, reply_markup=build_root_keyboard())


@router.message(F.text.in_(list(TEXT_TO_MODE_KEY.keys())))
async def on_mode_select(message: Message) -> None:
    session = get_session(message.from_user.id)
    mode_key = TEXT_TO_MODE_KEY[message.text]
    session.active_mode = mode_key
    session.ui_screen = UI_MODES

    text_map = {
        "universal": "Универсальный: гибкий ассистент на любую тему.",
        "medical": (
            "Медицина: осторожные, структурированные ответы с акцентом на безопасность. "
            "Не заменяет очный приём."
        ),
        "mentor": (
            "Наставник: фокус на личном росте, рефлексии и конкретных шагах."
        ),
        "business": (
            "Бизнес: цифры, гипотезы, риск/выгода, тестирование идей и продуктов."
        ),
        "creative": (
            "Креатив: идеи, формулировки, концепции, необычные ходы."
        ),
    }
    description = text_map.get(mode_key, "Режим обновлён.")

    await message.answer(
        f"Режим переключён: {mode_key}.\n\n{description}",
        reply_markup=build_modes_keyboard(),
    )


# ---------- Команды "продолжи" / "подробнее" ----------

@router.message(F.text.regexp(r"(?i)^\s*продолж(и|ить)\s*$"))
async def on_continue(message: Message) -> None:
    chat_id = message.chat.id
    session = get_session(message.from_user.id)

    last = LAST_ANSWERS.get(chat_id)
    if not last or not last.meta.get("truncated"):
        await message.answer(
            "Предыдущий ответ уже полный. Если нужен новый разбор — задай новый вопрос.",
            reply_markup=build_root_keyboard(),
        )
        return

    answer = await stream_answer(
        message=message,
        mode_key=session.active_mode,
        user_text="Продолжи, пожалуйста, предыдущий ответ.",
        history=session.history,
        style_hint=None,
        force_mode="deep",
    )

    assistant_text = answer.meta.get("full_text") or answer.full_text
    update_history(
        session,
        user_prompt="Продолжи предыдущий ответ.",
        assistant_text=assistant_text,
    )


@router.message(F.text.regexp(r"(?i)^\s*(подробнее|раскрой|раскрыть подробнее)\s*$"))
async def on_expand_text(message: Message) -> None:
    session = get_session(message.from_user.id)

    answer = await stream_answer(
        message=message,
        mode_key=session.active_mode,
        user_text="Раскрой подробнее предыдущий ответ.",
        history=session.history,
        style_hint=None,
        force_mode="deep",
    )

    assistant_text = answer.meta.get("full_text") or answer.full_text
    update_history(
        session,
        user_prompt="Раскрой подробнее предыдущий ответ.",
        assistant_text=assistant_text,
    )


# ---------- Главный диалоговый хендлер ----------

@router.message(F.text)
async def on_user_message(message: Message) -> None:
    """
    Главный диалог:
    - учитываем текущий режим (универсальный / мед / наставник / бизнес / креатив),
    - пробрасываем историю,
    - запускаем живое печатание.
    """
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напиши, что тебя волнует или интересует.", reply_markup=build_root_keyboard())
        return

    # Команды и текст из таскбара здесь не обрабатываем
    if text.startswith("/"):
        return
    if text in MENU_BUTTON_TEXTS:
        return

    session = get_session(message.from_user.id)

    answer = await stream_answer(
        message=message,
        mode_key=session.active_mode,
        user_text=text,
        history=session.history,
        style_hint=None,
    )

    assistant_text = answer.meta.get("full_text") or answer.full_text
    update_history(session, user_prompt=text, assistant_text=assistant_text)


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
