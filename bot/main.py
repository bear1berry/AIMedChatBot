import asyncio
import logging
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.filters.command import CommandObject
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.chat_action import ChatActionSender

from bot.config import (
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

storage = Storage()
router = Router()

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
    BTN_SUB_1M,
    BTN_SUB_3M,
    BTN_SUB_12M,
    BTN_BACK,
}


# =========================
#   Клавиатуры
# =========================


def build_root_keyboard() -> ReplyKeyboardMarkup:
    """
    Главный таскбар: 4 «капсулы» как на доке iOS.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_HOME_MODES), KeyboardButton(text=BTN_HOME_PROFILE)],
            [KeyboardButton(text=BTN_HOME_SUBSCRIPTION), KeyboardButton(text=BTN_HOME_REFERRALS)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Напиши запрос или пользуйся таскбаром ↓",
        one_time_keyboard=False,
        is_persistent=True,
    )


def build_modes_keyboard() -> ReplyKeyboardMarkup:
    """
    Экран «Режимы»: проваливаемся на уровень ниже.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_MODE_UNIVERSAL), KeyboardButton(text=BTN_MODE_MED)],
            [KeyboardButton(text=BTN_MODE_MENTOR), KeyboardButton(text=BTN_MODE_BUSINESS)],
            [KeyboardButton(text=BTN_MODE_CREATIVE), KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери режим ↓",
        one_time_keyboard=False,
        is_persistent=True,
    )


def build_subscription_keyboard() -> ReplyKeyboardMarkup:
    """
    Экран «Подписка»: выбор тарифа только через таскбар.
    """
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

    # реферал из /start <code>
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
            "• Пиши запрос внизу — отвечаю в реальном времени.\n"
            "• Управление — только через 4 кнопки таскбара.\n\n"
            "Попробуй что-нибудь простое, например: «Собери режим дня» или «Разберём мою идею стартапа?»"
        )
    else:
        text = (
            "🖤 <b>BlackBox GPT — Universal AI Assistant</b>\n\n"
            f"Режим: <b>{mode_label}</b>\n"
            f"Лимит на сегодня: <b>{limits['used_today']} / {limits['limit_today']}</b>\n\n"
            "Я здесь. Просто сформулируй задачу — остальное возьму на себя."
        )

    await message.answer(text, reply_markup=build_root_keyboard())


# =========================
#   Режимы (проваливание через таскбар)
# =========================


@router.message(F.text == BTN_HOME_MODES)
async def on_modes_entry(message: Message) -> None:
    """
    Нажали «Режимы» на главном экране — показываем минимальный текст
    и меняем таскбар на список режимов.
    """
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
        "Выбери новый режим внизу. Кнопка «Назад» вернёт на главный экран."
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
        f"Режим переключён на <b>{mode_label}</b>.\n\nПиши запрос — я адаптирую стиль ответа под него.",
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


# =========================
#   Профиль (план + лимиты + память)
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

    text = (
        "👤 <b>Профиль</b>\n\n"
        f"Тариф: {plan_line}\n"
        f"Режим: <b>{mode_label}</b>\n"
        f"Сегодня: <b>{limits['used_today']} / {limits['limit_today']}</b> запросов\n"
        f"Всего: <b>{limits['total_requests']}</b>\n\n"
        "🧠 <b>Память (краткий срез)</b>\n\n"
        f"{dossier_preview}"
    )

    await message.answer(text, reply_markup=build_root_keyboard())


# =========================
#   Подписка (Premium через CryptoBot, выбор тарифа в таскбаре)
# =========================


@router.message(F.text == BTN_HOME_SUBSCRIPTION)
async def on_subscription_entry(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    user_id = user.id

    plan_info = storage.get_plan_info(user_id)
    limits = storage.get_limits(user_id)

    plan_title = plan_info["plan_title"]
    expires = plan_info["plan_expires_at"]

    if plan_info["plan"] == "free":
        current_line = f"Текущий тариф: <b>{plan_title}</b>\n"
    else:
        current_line = f"Текущий тариф: <b>{plan_title}</b>\n"
        if expires:
            current_line += f"Активен до: <b>{expires}</b>\n"

    if not CRYPTO_PAY_API_TOKEN:
        text = (
            "💎 <b>Подписка</b>\n\n"
            f"{current_line}"
            f"Лимит сегодня: <b>{limits['used_today']} / {limits['limit_today']}</b>\n\n"
            "Оплата через CryptoBot ещё не настроена.\n"
            "Добавь CRYPTO_PAY_API_TOKEN в .env и перезапусти бота."
        )
        await message.answer(text, reply_markup=build_root_keyboard())
        return

    text = (
        "💎 <b>Premium</b>\n\n"
        f"{current_line}"
        f"Твой лимит сегодня: <b>{limits['used_today']} / {limits['limit_today']}</b>\n\n"
        "Выбери тариф внизу. Оплата — только USDT через @CryptoBot.\n"
        "После подтверждения оплаты подписка включится автоматически."
    )
    await message.answer(text, reply_markup=build_subscription_keyboard())


async def _create_invoice_and_wait(message: Message, tariff_key: str) -> None:
    user = message.from_user
    if not user:
        return
    user_id = user.id
    chat_id = message.chat.id

    tariff = SUBSCRIPTION_TARIFFS.get(tariff_key)
    if not tariff:
        await message.answer("Этот тариф временно недоступен.", reply_markup=build_root_keyboard())
        return

    if not CRYPTO_PAY_API_TOKEN:
        await message.answer("Оплата через CryptoBot не настроена.", reply_markup=build_root_keyboard())
        return

    try:
        invoice_id, pay_url = await create_cryptobot_invoice(user_id, tariff_key)
    except Exception as e:  # noqa: BLE001
        log.exception("Failed to create CryptoBot invoice: %s", e)
        await message.answer("Не удалось создать счёт. Попробуй ещё раз чуть позже.", reply_markup=build_root_keyboard())
        return

    storage.register_invoice(
        user_id=user_id,
        invoice_id=invoice_id,
        plan="premium",
        duration_days=tariff["duration_days"],
    )

    text = (
        f"Счёт создан: <b>{tariff['title']}</b> за <b>{tariff['amount']:.2f} USDT</b>.\n\n"
        f"Оплати по ссылке:\n{pay_url}\n\n"
        "Я буду периодически проверять статус счёта и включу Premium, как только CryptoBot подтвердит оплату."
    )

    await message.answer(text, reply_markup=build_root_keyboard())

    asyncio.create_task(
        _wait_for_payment_and_activate(user_id, chat_id, invoice_id, tariff_key)
    )


async def _wait_for_payment_and_activate(
    user_id: int,
    chat_id: int,
    invoice_id: int,
    tariff_key: str,
    check_interval: int = 20,
    max_checks: int = 24,
) -> None:
    from aiogram import Bot as AiogramBot

    bot = AiogramBot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    tariff = SUBSCRIPTION_TARIFFS.get(tariff_key) or {}
    title = tariff.get("title", "Premium")
    duration_days = tariff.get("duration_days", 30)

    for _ in range(max_checks):
        try:
            status = await get_invoice_status(invoice_id)
        except Exception as e:  # noqa: BLE001
            log.exception("Error checking CryptoBot invoice: %s", e)
            await asyncio.sleep(check_interval)
            continue

        if status == "paid":
            storage.update_invoice_status(user_id, invoice_id, "paid")
            storage.set_plan(user_id, "premium", duration_days)
            plan_info = storage.get_plan_info(user_id)
            expires = plan_info.get("plan_expires_at")

            text = (
                "✅ Оплата через CryptoBot подтверждена.\n\n"
                f"Тариф: <b>{title}</b> активирован.\n"
            )
            if expires:
                text += f"Подписка действует до: <b>{expires}</b>.\n\n"
            else:
                text += "\n"

            text += "Можешь загружать меня по полной 😉"

            await bot.send_message(chat_id, text, reply_markup=build_root_keyboard())
            return

        if status in {"expired", "cancelled"}:
            storage.update_invoice_status(user_id, invoice_id, status)
            await bot.send_message(
                chat_id,
                f"Статус счёта: <b>{status}</b>.\n"
                "Если оплату не успел — просто оформи новый тариф из раздела «Подписка».",
                reply_markup=build_root_keyboard(),
            )
            return

        await asyncio.sleep(check_interval)

    await bot.send_message(
        chat_id,
        "❓ За отведённое время я не увидел подтверждения оплаты от CryptoBot.\n"
        "Если ты уверен, что оплатил — напиши админу или повторно оформи подписку.",
        reply_markup=build_root_keyboard(),
    )


@router.message(F.text == BTN_SUB_1M)
async def btn_sub_1m(message: Message) -> None:
    await _create_invoice_and_wait(message, "premium_1m")


@router.message(F.text == BTN_SUB_3M)
async def btn_sub_3m(message: Message) -> None:
    await _create_invoice_and_wait(message, "premium_3m")


@router.message(F.text == BTN_SUB_12M)
async def btn_sub_12m(message: Message) -> None:
    await _create_invoice_and_wait(message, "premium_12m")


# =========================
#   Рефералы
# =========================


@router.message(F.text == BTN_HOME_REFERRALS)
async def on_referrals(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    user_id = user.id

    info = storage.get_referral_info(user_id)

    if BOT_USERNAME:
        ref_link = f"https://t.me/{BOT_USERNAME}?start={info['code']}"
    else:
        ref_link = info["code"]

    text = (
        "👥 <b>Рефералы</b>\n\n"
        "Поделись ссылкой с теми, кому реально нужен сильный ИИ-ассистент.\n\n"
        f"Твоя ссылка:\n{ref_link}\n\n"
        f"Приглашено: <b>{info['invited_count']}</b>\n"
        f"Базовый лимит: <b>{info['base_limit']}</b>\n"
        f"Бонус от рефералов: <b>{info['ref_bonus']}</b>\n"
        f"Итого лимит в день: <b>{info['limit_today']}</b>"
    )

    await message.answer(text, reply_markup=build_root_keyboard())


# =========================
#   Назад на главный экран
# =========================


@router.message(F.text == BTN_BACK)
async def on_back(message: Message) -> None:
    text = (
        "🖤 <b>BlackBox GPT — Universal AI Assistant</b>\n\n"
        "Главный экран. Просто напиши запрос или воспользуйся таскбаром снизу."
    )
    await message.answer(text, reply_markup=build_root_keyboard())


# =========================
#   Основной диалог (стрим)
# =========================


@router.message(
    F.text
    & ~F.text.startswith("/")
    & ~F.text.in_(ALL_MENU_BUTTONS)
)
async def handle_chat(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    user_id = user.id

    prompt = (message.text or "").strip()
    if not prompt:
        return

    # Лимиты
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
        "Добавь его в переменную окружения <b>ADMIN_USER_IDS</b>, "
        "чтобы включить для себя админ-режим без лимитов.",
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


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "<b>Навигация</b>\n\n"
        f"{BTN_HOME_MODES} — выбор режима\n"
        f"{BTN_HOME_PROFILE} — тариф, лимиты и память\n"
        f"{BTN_HOME_SUBSCRIPTION} — оформление Premium\n"
        f"{BTN_HOME_REFERRALS} — реферальная ссылка и бонусы\n\n"
        "Дальше всё просто: минимум интерфейса, максимум пользы."
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
