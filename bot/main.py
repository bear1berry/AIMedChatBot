import asyncio
import logging
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.filters.command import CommandObject
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.chat_action import ChatActionSender

from bot.config import (
    BASE_DIR,
    BOT_TOKEN,
    ASSISTANT_MODES,
    DEFAULT_MODE_KEY,
    PLAN_LIMITS,
    SUBSCRIPTION_TARIFFS,
    CRYPTO_PAY_API_TOKEN,
    BOT_USERNAME,
)
from services.llm import ask_llm_stream
from services.storage import Storage
from services.payments import create_cryptobot_invoice, get_invoice_status
from services.audio import speech_to_text, text_to_speech

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

storage = Storage()
router = Router()

# set of users, кому озвучиваем ответы
VOICE_REPLY_USERS: set[int] = set()

# =========================
#   Тексты кнопок (нижний таскбар)
# =========================

BTN_HOME_MODES = "🧠 Режимы"
BTN_HOME_PROFILE = "👤 Профиль"
BTN_HOME_SUBSCRIPTION = "💎 Подписка"
BTN_HOME_REFERRALS = "👥 Рефералы"

# кнопки второго уровня (режимы)
BTN_MODE_UNIVERSAL = "🧠 Универсальный"
BTN_MODE_MED = "🩺 Медицина"
BTN_MODE_MENTOR = "🔥 Наставник"
BTN_MODE_BUSINESS = "💼 Бизнес"
BTN_MODE_CREATIVE = "🎨 Креатив"
BTN_MODE_VOICE_COACH = "🎧 Голосовой коуч"

# кнопки второго уровня (подписка)
BTN_SUB_1M = "💎 1 месяц"
BTN_SUB_3M = "💎 3 месяца"
BTN_SUB_12M = "💎 12 месяцев"

# назад
BTN_BACK = "⬅️ Назад"

ROOT_BUTTONS = {BTN_HOME_MODES, BTN_HOME_PROFILE, BTN_HOME_SUBSCRIPTION, BTN_HOME_REFERRALS}

ALL_MENU_BUTTONS = ROOT_BUTTONS | {
    BTN_MODE_UNIVERSAL,
    BTN_MODE_MED,
    BTN_MODE_MENTOR,
    BTN_MODE_BUSINESS,
    BTN_MODE_CREATIVE,
    BTN_MODE_VOICE_COACH,
    BTN_SUB_1M,
    BTN_SUB_3M,
    BTN_SUB_12M,
    BTN_BACK,
}


# =========================
#   Клавиатуры
# =========================


def build_root_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_HOME_MODES), KeyboardButton(text=BTN_HOME_PROFILE)],
            [KeyboardButton(text=BTN_HOME_SUBSCRIPTION), KeyboardButton(text=BTN_HOME_REFERRALS)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Напиши запрос или отправь голосовое ↓",
        one_time_keyboard=False,
        is_persistent=True,
    )


def build_modes_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_MODE_UNIVERSAL), KeyboardButton(text=BTN_MODE_MED)],
            [KeyboardButton(text=BTN_MODE_MENTOR), KeyboardButton(text=BTN_MODE_BUSINESS)],
            [KeyboardButton(text=BTN_MODE_CREATIVE), KeyboardButton(text=BTN_MODE_VOICE_COACH)],
            [KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери режим ↓",
        one_time_keyboard=False,
        is_persistent=True,
    )


def build_subscription_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SUB_1M), KeyboardButton(text=BTN_SUB_3M)],
            [KeyboardButton(text=BTN_SUB_12M), KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери тариф ↓",
        one_time_keyboard=False,
        is_persistent=True,
    )


# =========================
#   /start + онбординг
# =========================


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject) -> None:
    user = message.from_user
    if not user:
        return
    user_id = user.id

    if command.args:
        storage.register_referral(user_id, command.args.strip())

    user_data, is_new = storage.get_or_create_user(user_id)
    limits = storage.get_limits(user_id)
    mode_key = storage.get_mode(user_id)
    mode_cfg = ASSISTANT_MODES.get(mode_key, ASSISTANT_MODES[DEFAULT_MODE_KEY])
    mode_label = f"{mode_cfg.get('emoji', '')} {mode_cfg.get('title', mode_key)}".strip()

    if is_new or not user_data.get("onboarding_seen"):
        storage.mark_onboarding_seen(user_id)
        text = (
            "🖤 <b>BlackBox GPT — Universal AI Assistant</b>\n\n"
            "Минималистичный ИИ-ассистент без лишних кнопок.\n\n"
            "• Пиши запрос текстом или отправляй голосовые.\n"
            "• Управление — только через 4 кнопки таскбара.\n\n"
            "Попробуй: «Разбери мой день и выдели задачи» или просто наговори голосовое."
        )
    else:
        text = (
            "🖤 <b>BlackBox GPT — Universal AI Assistant</b>\n\n"
            f"Режим: <b>{mode_label}</b>\n"
            f"Лимит на сегодня: <b>{limits['used_today']} / {limits['limit_today']}</b>\n\n"
            "Я здесь. Пиши или говори — я разберу."
        )

    await message.answer(text, reply_markup=build_root_keyboard())


# =========================
#   Режимы
# =========================


@router.message(F.text == BTN_HOME_MODES)
async def on_modes_entry(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    user_id = user.id

    mode_key = storage.get_mode(user_id)
    mode_cfg = ASSISTANT_MODES.get(mode_key, ASSISTANT_MODES[DEFAULT_MODE_KEY])
    mode_label = f"{mode_cfg.get('emoji', '')} {mode_cfg.get('title', mode_key)}".strip()

    text = (
        "⚙️ <b>Режимы мышления</b>\n\n"
        f"Сейчас: <b>{mode_label}</b>\n\n"
        "Выбери новый режим внизу. «Голосовой коуч» заточен под прогулочные голосовые сессии."
    )
    await message.answer(text, reply_markup=build_modes_keyboard())


async def _set_mode_and_back_to_root(message: Message, mode_key: str) -> None:
    user = message.from_user
    if not user:
        return
    user_id = user.id

    if mode_key not in ASSISTANT_MODES:
        await message.answer("Такого режима нет.", reply_markup=build_root_keyboard())
        return

    storage.update_mode(user_id, mode_key)
    mode_cfg = ASSISTANT_MODES[mode_key]
    mode_label = f"{mode_cfg.get('emoji', '')} {mode_cfg.get('title', mode_key)}".strip()

    await message.answer(
        f"Режим переключён на <b>{mode_label}</b>.\n\nПиши или отправляй голосовые — я адаптирую стиль работы.",
        reply_markup=build_root_keyboard(),
    )


@router.message(F.text == BTN_MODE_UNIVERSAL)
async def btn_mode_universal(message: Message) -> None:
    await _set_mode_and_back_to_root(message, "universal")


@router.message(F.text == BTN_MODE_MED)
async def btn_mode_med(message: Message) -> None:
    await _set_mode_and_back_to_root(message, "med")


@router.message(F.text == BTN_MODE_MENTOR)
async def btn_mode_mentor(message: Message) -> None:
    await _set_mode_and_back_to_root(message, "mentor")


@router.message(F.text == BTN_MODE_BUSINESS)
async def btn_mode_business(message: Message) -> None:
    await _set_mode_and_back_to_root(message, "business")


@router.message(F.text == BTN_MODE_CREATIVE)
async def btn_mode_creative(message: Message) -> None:
    await _set_mode_and_back_to_root(message, "creative")


@router.message(F.text == BTN_MODE_VOICE_COACH)
async def btn_mode_voice_coach(message: Message) -> None:
    await _set_mode_and_back_to_root(message, "voice_coach")


# =========================
#   Профиль
# =========================


@router.message(F.text == BTN_HOME_PROFILE)
async def on_profile(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    user_id = user.id

    limits = storage.get_limits(user_id)
    plan_info = storage.get_plan_info(user_id)
    mode_key = storage.get_mode(user_id)
    mode_cfg = ASSISTANT_MODES.get(mode_key, ASSISTANT_MODES[DEFAULT_MODE_KEY])
    mode_label = f"{mode_cfg.get('emoji', '')} {mode_cfg.get('title', mode_key)}".strip()

    plan_title = plan_info["plan_title"]
    expires = plan_info["plan_expires_at"]

    if plan_info["plan"] == "free":
        plan_line = f"<b>{plan_title}</b>"
    else:
        plan_line = f"<b>{plan_title}</b>"
        if expires:
            plan_line += f" · до <b>{expires}</b>"

    dossier_preview = storage.get_dossier_preview(user_id)

    voice_reply = "включен" if user_id in VOICE_REPLY_USERS else "выключен"

    text = (
        "👤 <b>Профиль</b>\n\n"
        f"Тариф: {plan_line}\n"
        f"Режим: <b>{mode_label}</b>\n"
        f"Сегодня: <b>{limits['used_today']} / {limits['limit_today']}</b> запросов\n"
        f"Всего: <b>{limits['total_requests']}</b>\n"
        f"Голосовые ответы: <b>{voice_reply}</b> "
        "(/voice_reply_on, /voice_reply_off)\n\n"
        "🧠 <b>Память (краткий срез)</b>\n\n"
        f"{dossier_preview}"
    )

    await message.answer(text, reply_markup=build_root_keyboard())


# =========================
#   Подписка
# =========================
# (тот же код, что мы делали ранее — я его не меняю, только оставляю как есть
#  с Cryptobot и тарифами premium_1m/3m/12m)
# ------------- ТУТ ИДЁТ ТВОЙ СУЩЕСТВУЮЩИЙ КОД ПОДПИСКИ ------------- #
# Я его не разворачиваю заново, чтобы не раздувать ответ.
# Просто оставь блок "Подписка" из последней версии main.py без изменений.
# -------------------------------------------------------------------


# =========================
#   Рефералы
# =========================
# (аналогично — блок рефералов из последней версии main.py)


# =========================
#   Назад
# =========================


@router.message(F.text == BTN_BACK)
async def on_back(message: Message) -> None:
    text = (
        "🖤 <b>BlackBox GPT — Universal AI Assistant</b>\n\n"
        "Главный экран. Пиши запрос или отправь голосовое — я разберу."
    )
    await message.answer(text, reply_markup=build_root_keyboard())


# =========================
#   Общая обработка запросов (текст/голос)
# =========================


async def _process_prompt(message: Message, prompt: str, source: str = "text") -> None:
    user = message.from_user
    if not user:
        return
    user_id = user.id

    if not prompt.strip():
        await message.answer("Не нашёл текста в запросе.", reply_markup=build_root_keyboard())
        return

    if not storage.can_make_request(user_id):
        limits = storage.get_limits(user_id)
        text = (
            "На сегодня лимит запросов исчерпан.\n\n"
            f"Сделано: <b>{limits['used_today']} / {limits['limit_today']}</b>.\n\n"
            "Можно подождать до завтра или оформить Premium в разделе «Подписка»."
        )
        await message.answer(text, reply_markup=build_root_keyboard())
        return

    mode_key = storage.get_mode(user_id)
    history = storage.get_history(user_id)

    storage.append_history(user_id, "user", prompt)
    storage.update_dossier_on_message(user_id, mode_key, prompt)
    storage.increment_usage(user_id)

    bot = message.bot
    sent = await message.answer("🧠 Думаю над ответом…")

    reply_text = ""
    last_edit = datetime.now()

    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        async for chunk in ask_llm_stream(mode_key, prompt, history):
            reply_text += chunk
            now = datetime.now()
            if (now - last_edit).total_seconds() > 0.7 and reply_text:
                view = reply_text[-4096:]
                try:
                    await sent.edit_text(view)
                except Exception:
                    pass
                last_edit = now

    if reply_text:
        view = reply_text[-4096:]
        try:
            await sent.edit_text(view)
        except Exception:
            await sent.edit_text("Ответ сформирован, но не удалось отрисовать текст.")
    else:
        await sent.edit_text("Не получилось получить ответ от модели. Попробуй ещё раз.")

    storage.append_history(user_id, "assistant", reply_text)

    # Опциональный голосовой ответ
    if user_id in VOICE_REPLY_USERS and reply_text.strip():
        try:
            file_name = f"tts_{user_id}_{message.message_id}.ogg"
            voice_path = await text_to_speech(reply_text, file_name=file_name)
            await message.answer_voice(voice_path.open("rb"))
        except Exception as e:  # noqa: BLE001
            log.exception("TTS error: %s", e)


@router.message(
    F.text
    & ~F.text.startswith("/")
    & ~F.text.in_(ALL_MENU_BUTTONS)
)
async def handle_chat(message: Message) -> None:
    prompt = (message.text or "").strip()
    await _process_prompt(message, prompt, source="text")


@router.message(F.voice)
async def handle_voice(message: Message) -> None:
    """
    Прогулочная сессия: пользователь шлёт voice, мы:
    1) скачиваем файл;
    2) через SpeechKit STT вытаскиваем текст;
    3) прогоняем через то же ядро, что и текст.
    """
    user = message.from_user
    if not user:
        return
    user_id = user.id

    try:
        file = await message.bot.get_file(message.voice.file_id)
        tmp_dir = BASE_DIR / "tmp" / "voice"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        ogg_path = tmp_dir / f"voice_{user_id}_{message.message_id}.ogg"
        await message.bot.download_file(file.file_path, destination=str(ogg_path))

        text = await speech_to_text(ogg_path)
    except Exception as e:  # noqa: BLE001
        log.exception("Voice/STT error: %s", e)
        await message.answer(
            "Не получилось распознать голосовое. "
            "Возможно, оно слишком длинное или сервис временно недоступен.",
            reply_markup=build_root_keyboard(),
        )
        return

    if not text:
        await message.answer("Я не смог распознать текст в голосовом сообщении.", reply_markup=build_root_keyboard())
        return

    await _process_prompt(message, text, source="voice")


# =========================
#   Сервисные команды
# =========================


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    await message.answer(
        f"Твой Telegram ID: <code>{user.id}</code>\n\n"
        "Добавь его в ADMIN_USER_IDS в .env, чтобы включить админ-режим.",
        reply_markup=build_root_keyboard(),
    )


@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    user_id = user.id

    storage.clear_history(user_id)
    await message.answer(
        "Диалог очищен. Начинаем ветку с нуля.",
        reply_markup=build_root_keyboard(),
    )


@router.message(Command("voice_reply_on"))
async def cmd_voice_on(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    VOICE_REPLY_USERS.add(user.id)
    await message.answer("Теперь ответы будут дублироваться голосом 🔊", reply_markup=build_root_keyboard())


@router.message(Command("voice_reply_off"))
async def cmd_voice_off(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    VOICE_REPLY_USERS.discard(user.id)
    await message.answer("Голосовые ответы выключены.", reply_markup=build_root_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "<b>Навигация</b>\n\n"
        f"{BTN_HOME_MODES} — выбор режима (в т.ч. 🎧 Голосовой коуч)\n"
        f"{BTN_HOME_PROFILE} — тариф, лимиты и память\n"
        f"{BTN_HOME_SUBSCRIPTION} — оформление Premium\n"
        f"{BTN_HOME_REFERRALS} — реферальная ссылка и бонусы\n\n"
        "Команды:\n"
        "/voice_reply_on — включить озвучку ответов\n"
        "/voice_reply_off — выключить озвучку\n"
        "/reset — очистить диалог\n"
        "/id — показать твой Telegram ID"
    )
    await message.answer(text, reply_markup=build_root_keyboard())


# =========================
#   Запуск бота
# =========================


async def main() -> None:
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    log.info("Starting BlackBox bot polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
