import asyncio
import logging
import os
from typing import Dict, Optional

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

# =========================
#  Configuration
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in environment variables")

if not DEEPSEEK_API_KEY:
    raise RuntimeError("DEEPSEEK_API_KEY is not set in environment variables")

DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

# =========================
#  Assistant modes
# =========================

ASSISTANT_MODES: Dict[str, Dict[str, str]] = {
    "universal": {
        "title": "🧠 Универсальный",
        "description": "Ответы на любые вопросы: от жизни до кода.",
        "system_prompt": (
            "Ты — мощный русскоязычный универсальный ИИ-ассистент. "
            "Отвечай максимально полезно, структурированно и по делу. "
            "Сохраняй дружелюбный, но уверенный тон. "
            "При необходимости используй списки и шаги."
        ),
    },
    "med": {
        "title": "⚕️ Медицина",
        "description": "Профильный режим для медицины и доказательной базы.",
        "system_prompt": (
            "Ты — ИИ-ассистент врача-эпидемиолога с уклоном в доказательную медицину. "
            "Не ставь диагнозы и не назначай лечение — всегда напоминай, что нужна очная консультация врача. "
            "Объясняй механизмы болезней, препараты и исследования простым языком, но научно корректно."
        ),
    },
    "coach": {
        "title": "🔥 Наставник",
        "description": "Дисциплина, цели, прокачка личности.",
        "system_prompt": (
            "Ты — личный наставник по развитию личности, дисциплине и продуктивности. "
            "Помогай выстраивать систему, а не только давать мотивацию. "
            "Будь прямолинейным, но поддерживающим. "
            "Фокус на конкретных шагах и привычках."
        ),
    },
    "biz": {
        "title": "💼 Бизнес / Идеи",
        "description": "Стратегия, Telegram, стартапы, монетизация.",
        "system_prompt": (
            "Ты — стратег по цифровым продуктам и Telegram-проектам. "
            "Помогаешь продумывать монетизацию, воронки, UX и автоматизацию с помощью ИИ. "
            "Отвечай структурно: блоки, шаги, приоритеты."
        ),
    },
    "creative": {
        "title": "🎨 Креатив",
        "description": "Нейминг, тексты, промпты, визуальные концепции.",
        "system_prompt": (
            "Ты — креативный директор и копирайтер. "
            "Генерируешь названия, тексты, образы, сильные промпты для генерации картинок. "
            "Сочетай дерзость, минимализм и премиальный стиль."
        ),
    },
}

DEFAULT_MODE_KEY = "universal"

# =========================
#  In-memory user state
# =========================


class UserState:
    def __init__(self, mode_key: str = DEFAULT_MODE_KEY) -> None:
        self.mode_key = mode_key
        self.last_prompt: Optional[str] = None
        self.last_answer: Optional[str] = None
        self.ref_code: Optional[str] = None  # заглушка под реферальную систему


user_states: Dict[int, UserState] = {}

# =========================
#  Keyboards (нижний таскбар)
# =========================


def build_main_keyboard(active_mode_key: str) -> InlineKeyboardMarkup:
    """
    Нижний таскбар: режимы ассистента + сервисные кнопки.
    Никаких ReplyKeyboard — только inline.
    """
    mode_buttons = [
        InlineKeyboardButton(
            text=("• " + cfg["title"] if key == active_mode_key else cfg["title"]),
            callback_data=f"mode:{key}",
        )
        for key, cfg in ASSISTANT_MODES.items()
    ]

    service_buttons = [
        InlineKeyboardButton(text="⚡ Сценарии", callback_data="service:templates"),
        InlineKeyboardButton(text="👤 Профиль", callback_data="service:profile"),
        InlineKeyboardButton(text="🎁 Реферал", callback_data="service:referral"),
    ]

    # Первая строка — режимы
    # Вторая строка — сервисные
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            mode_buttons,      # строка с режимами
            service_buttons,   # строка сервисных кнопок
        ]
    )
    return keyboard


def get_user_state(user_id: int) -> UserState:
    if user_id not in user_states:
        user_states[user_id] = UserState()
    return user_states[user_id]


# =========================
#  DeepSeek client
# =========================


async def call_deepseek(system_prompt: str, user_prompt: str) -> str:
    """
    Вызов DeepSeek Chat Completion через совместимый с OpenAI формат.
    """
    url = f"{DEEPSEEK_BASE_URL.rstrip('/')}/chat/completions"

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.7,
        "top_p": 0.9,
        "stream": False,
    }

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError):
        logging.error("Unexpected DeepSeek response format: %s", data)
        return "Произошла ошибка при обработке ответа модели. Попробуй ещё раз."


# =========================
#  Routers & Handlers
# =========================

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    state = get_user_state(message.from_user.id)
    mode_cfg = ASSISTANT_MODES[state.mode_key]

    text = (
        "🖤 <b>BlackBoxGPT</b>\n\n"
        "Твой персональный ИИ-ассистент.\n"
        "Выбери режим внизу и просто напиши запрос.\n\n"
        f"Текущий режим: <b>{mode_cfg['title']}</b>\n"
        f"<i>{mode_cfg['description']}</i>"
    )

    await message.answer(
        text,
        reply_markup=build_main_keyboard(state.mode_key),
    )


@router.message(Command("mode"))
async def cmd_mode(message: Message) -> None:
    state = get_user_state(message.from_user.id)
    text_lines = ["Выбери режим ассистента:\n"]
    for key, cfg in ASSISTANT_MODES.items():
        prefix = "•" if key == state.mode_key else "–"
        text_lines.append(f"{prefix} {cfg['title']} — {cfg['description']}")
    await message.answer(
        "\n".join(text_lines),
        reply_markup=build_main_keyboard(state.mode_key),
    )


@router.callback_query(F.data.startswith("mode:"))
async def cb_change_mode(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    state = get_user_state(user_id)

    _, mode_key = callback.data.split(":", 1)
    if mode_key not in ASSISTANT_MODES:
        await callback.answer("Неизвестный режим", show_alert=True)
        return

    state.mode_key = mode_key
    mode_cfg = ASSISTANT_MODES[mode_key]

    new_text = (
        "Режим обновлён ✅\n\n"
        f"Текущий режим: <b>{mode_cfg['title']}</b>\n"
        f"<i>{mode_cfg['description']}</i>\n\n"
        "Теперь просто напиши свой запрос."
    )

    try:
        # Пытаемся отредактировать последнее сообщение бота
        await callback.message.edit_text(
            new_text,
            reply_markup=build_main_keyboard(state.mode_key),
        )
    except Exception:
        # Если не получилось — просто шлём новое
        await callback.message.answer(
            new_text,
            reply_markup=build_main_keyboard(state.mode_key),
        )

    await callback.answer("Режим переключен")


@router.callback_query(F.data.startswith("service:"))
async def cb_service(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    state = get_user_state(user_id)
    _, action = callback.data.split(":", 1)

    if action == "templates":
        text = (
            "⚡ <b>Быстрые сценарии</b>\n\n"
            "Например, можешь написать:\n"
            "• «Сделай структуру Telegram-канала по медицине»\n"
            "• «Придумай 10 идей постов для моего бота»\n"
            "• «Разбери мой день и предложи улучшения режима»\n\n"
            "Или просто напиши свою задачу — режим уже выбран."
        )
    elif action == "profile":
        text = (
            "👤 <b>Профиль</b>\n\n"
            "Пока профиль хранится в памяти бота в оперативке сервера.\n"
            "В следующих версиях тут будет:\n"
            "— Личные настройки режимов\n"
            "— Избранные сценарии\n"
            "— Статистика диалогов"
        )
    elif action == "referral":
        text = (
            "🎁 <b>Реферальная система</b>\n\n"
            "Здесь будет твоя персональная ссылка, за друзей — бонусы.\n"
            "Сейчас это заглушка, логика будет включена при запуске монетизации."
        )
    else:
        text = "Сервис в разработке."

    await callback.message.answer(
        text,
        reply_markup=build_main_keyboard(state.mode_key),
    )
    await callback.answer()


@router.message(F.text & ~F.via_bot & ~F.text.startswith("/"))
async def handle_text(message: Message) -> None:
    """
    Главный обработчик любых текстовых запросов пользователя.
    """
    user_id = message.from_user.id
    state = get_user_state(user_id)
    mode_cfg = ASSISTANT_MODES[state.mode_key]

    waiting_message = await message.answer(
        "⌛ Обрабатываю запрос в режиме "
        f"<b>{mode_cfg['title']}</b>...\n\nОбычно это занимает несколько секунд.",
        reply_markup=build_main_keyboard(state.mode_key),
    )

    user_prompt = message.text.strip()
    state.last_prompt = user_prompt

    try:
        answer = await call_deepseek(
            system_prompt=mode_cfg["system_prompt"],
            user_prompt=user_prompt,
        )
        state.last_answer = answer

        await waiting_message.edit_text(
            answer,
            reply_markup=build_main_keyboard(state.mode_key),
        )
    except httpx.HTTPStatusError as e:
        logging.exception("DeepSeek HTTP error: %s", e)
        await waiting_message.edit_text(
            "🚫 DeepSeek вернул ошибку. Попробуй ещё раз позже "
            "или проверь баланс / API-ключ.",
            reply_markup=build_main_keyboard(state.mode_key),
        )
    except Exception as e:  # noqa: BLE001
        logging.exception("Unexpected error while handling text: %s", e)
        await waiting_message.edit_text(
            "❌ Произошла неожиданная ошибка. Я уже в логах, можешь попробовать ещё раз.",
            reply_markup=build_main_keyboard(state.mode_key),
        )


# =========================
#  Entrypoint
# =========================


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
