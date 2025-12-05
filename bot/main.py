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
#   Нижний таскбар (4 кнопки)
# =========================

BTN_MODES = "🧠 Режимы"
BTN_PROFILE = "👤 Профиль"
BTN_SUBSCRIPTION = "💎 Подписка"
BTN_REFERRALS = "👥 Рефералы"

TASKBAR_BUTTONS = {BTN_MODES, BTN_PROFILE, BTN_SUBSCRIPTION, BTN_REFERRALS}


def build_main_keyboard() -> ReplyKeyboardMarkup:
    """
    Минималистичный таскбар в стиле iOS:
    4 кнопки, ровные ряды, без визуального мусора.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=BTN_MODES),
                KeyboardButton(text=BTN_PROFILE),
            ],
            [
                KeyboardButton(text=BTN_SUBSCRIPTION),
                KeyboardButton(text=BTN_REFERRALS),
            ],
        ],
        resize_keyboard=True,
        input_field_placeholder="Напиши запрос или выбери раздел ↓",
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

    # Реферальный код: /start <code>
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
            "🖤 <b>Black Box</b>\n\n"
            "Минимум кнопок, максимум мозга.\n\n"
            "Как со мной работать:\n"
            "1️⃣ Внизу выбери <b>Режимы</b> и посмотри команды переключения.\n"
            "2️⃣ Просто формулируй задачу человеческим языком.\n"
            "3️⃣ Если нужно больше мощности — загляни в <b>Подписка</b>.\n\n"
            "Таскбар — как док на iPhone: 4 «стеклянные» кнопки, всё остальное — через живой текст."
        )
    else:
        text = (
            f"Снова на связи, {user.first_name}.\n\n"
            f"Текущий режим: <b>{mode_label}</b>\n"
            f"Лимит на сегодня: <b>{limits['used_today']} / {limits['limit_today']}</b> запросов.\n\n"
            "Пиши, что нужно, или пользуйся таскбаром снизу."
        )

    await message.answer(text, reply_markup=build_main_keyboard())


# =========================
#   Режимы (через команды /mode_*)
# =========================

@router.message(F.text == BTN_MODES)
async def handle_modes_menu(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    user_id = user.id

    mode_key = storage.get_mode(user_id)
    mode_cfg = ASSISTANT_MODES.get(mode_key, ASSISTANT_MODES[DEFAULT_MODE_KEY])
    current_label = f"{mode_cfg.get('emoji', '')} {mode_cfg.get('title', mode_key)}".strip()

    text = (
        "⚙️ <b>Режимы мышления</b>\n\n"
        f"Сейчас: <b>{current_label}</b>\n\n"
        "Нажми на нужную команду, чтобы переключиться:\n"
        "• /mode_universal — 🧠 Универсальный\n"
        "• /mode_med — 🩺 Медицина\n"
        "• /mode_mentor — 🔥 Наставник\n"
        "• /mode_business — 💼 Бизнес\n"
        "• /mode_creative — 🎨 Креатив\n\n"
        "Можешь просто тапнуть по команде — Telegram сам подставит её в строку."
    )

    await message.answer(text)


async def _set_mode_and_reply(message: Message, mode_key: str) -> None:
    user = message.from_user
    if not user:
        return
    user_id = user.id

    if mode_key not in ASSISTANT_MODES:
        await message.answer("Такого режима нет.")
        return

    storage.update_mode(user_id, mode_key)
    mode_cfg = ASSISTANT_MODES[mode_key]
    limits = storage.get_limits(user_id)

    mode_label = f"{mode_cfg.get('emoji', '')} {mode_cfg.get('title', mode_key)}".strip()

    text = (
        f"Режим переключён на <b>{mode_label}</b>.\n\n"
        f"Сегодняшний лимит: <b>{limits['used_today']} / {limits['limit_today']}</b> запросов.\n\n"
        "Просто напиши задачу — я подстрою стиль ответа под выбранный режим."
    )

    await message.answer(text, reply_markup=build_main_keyboard())


@router.message(Command("mode_universal"))
async def mode_universal(message: Message) -> None:
    await _set_mode_and_reply(message, "universal")


@router.message(Command("mode_med"))
async def mode_med(message: Message) -> None:
    await _set_mode_and_reply(message, "med")


@router.message(Command("mode_mentor"))
async def mode_mentor(message: Message) -> None:
    await _set_mode_and_reply(message, "mentor")


@router.message(Command("mode_business"))
async def mode_business(message: Message) -> None:
    await _set_mode_and_reply(message, "business")


@router.message(Command("mode_creative"))
async def mode_creative(message: Message) -> None:
    await _set_mode_and_reply(message, "creative")


# =========================
#   Профиль (план + лимиты + память/досье)
# =========================

@router.message(F.text == BTN_PROFILE)
async def handle_profile(message: Message) -> None:
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
        plan_line = f"Тариф: <b>{plan_title}</b> (по умолчанию)"
    else:
        if expires:
            plan_line = f"Тариф: <b>{plan_title}</b>\nАктивен до: <b>{expires}</b>"
        else:
            plan_line = f"Тариф: <b>{plan_title}</b>"

    dossier_preview = storage.get_dossier_preview(user_id)

    text = (
        "👤 <b>Профиль</b>\n\n"
        f"{plan_line}\n"
        f"Режим: <b>{mode_label}</b>\n\n"
        f"Лимит сегодня: <b>{limits['used_today']} / {limits['limit_today']}</b>\n"
        f"Всего запросов: <b>{limits['total_requests']}</b>\n\n"
        "🧠 <b>Память</b>\n\n"
        f"{dossier_preview}"
    )

    await message.answer(text)


# =========================
#   Подписка (Premium через CryptoBot)
# =========================

@router.message(F.text == BTN_SUBSCRIPTION)
async def handle_subscription(message: Message) -> None:
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
        if expires:
            current_line = f"Текущий тариф: <b>{plan_title}</b>, активен до <b>{expires}</b>\n"
        else:
            current_line = f"Текущий тариф: <b>{plan_title}</b>\n"

    if not CRYPTO_PAY_API_TOKEN:
        text = (
            "💎 <b>Подписка</b>\n\n"
            f"{current_line}"
            f"Лимит сегодня: <b>{limits['used_today']} / {limits['limit_today']}</b>\n\n"
            "Сейчас оплата через CryptoBot ещё не настроена.\n"
            "Добавь CRYPTO_PAY_API_TOKEN в .env и перезапусти бота."
        )
        await message.answer(text)
        return

    # Берём цены из конфигурации
    t1 = SUBSCRIPTION_TARIFFS.get("premium_1m")
    t3 = SUBSCRIPTION_TARIFFS.get("premium_3m")
    t12 = SUBSCRIPTION_TARIFFS.get("premium_12m")

    lines = [
        "💎 <b>Premium-подписка</b>\n",
        current_line,
        f"Твой лимит сегодня: <b>{limits['used_today']} / {limits['limit_today']}</b>\n",
        "Оплата — только в USDT через <b>@CryptoBot</b>.\n",
        "Тарифы:\n",
    ]

    if t1:
        lines.append(
            f"• /premium_1m — {t1['title']} за <b>{t1['amount']:.2f} USDT</b>\n"
        )
    if t3:
        lines.append(
            f"• /premium_3m — {t3['title']} за <b>{t3['amount']:.2f} USDT</b>\n"
        )
    if t12:
        lines.append(
            f"• /premium_12m — {t12['title']} за <b>{t12['amount']:.2f} USDT</b>\n"
        )

    lines.append(
        "\nНажми на нужную команду /premium_… — я создам счёт в CryptoBot и дам ссылку.\n"
        "После оплаты я сам периодически проверю платёж и автоматически активирую подписку."
    )

    await message.answer("".join(lines))


async def _create_invoice_and_wait(
    message: Message,
    tariff_key: str,
) -> None:
    """
    1) Создаёт счёт в CryptoBot.
    2) Присылает ссылку на оплату.
    3) В фоне периодически проверяет статус и при оплате включает Premium.
    """
    user = message.from_user
    if not user:
        return
    user_id = user.id
    chat_id = message.chat.id

    tariff = SUBSCRIPTION_TARIFFS.get(tariff_key)
    if not tariff:
        await message.answer("Этот тариф временно недоступен.")
        return

    if not CRYPTO_PAY_API_TOKEN:
        await message.answer("Оплата через CryptoBot не настроена.")
        return

    try:
        invoice_id, pay_url = await create_cryptobot_invoice(user_id, tariff_key)
    except Exception as e:  # noqa: BLE001
        log.exception("Failed to create CryptoBot invoice: %s", e)
        await message.answer("Не удалось создать счёт. Попробуй ещё раз чуть позже.")
        return

    # Регистрируем счёт в сторедже
    storage.register_invoice(
        user_id=user_id,
        invoice_id=invoice_id,
        plan="premium",
        duration_days=tariff["duration_days"],
    )

    text = (
        f"Счёт создан: <b>{tariff['title']}</b> за <b>{tariff['amount']:.2f} USDT</b>.\n\n"
        f"1) Перейди по ссылке и оплати:\n{pay_url}\n\n"
        "2) После успешной оплаты просто вернись в бот.\n\n"
        "Я сам буду периодически проверять статус счёта и включу Premium автоматически, "
        "как только CryptoBot подтвердит оплату."
    )

    await message.answer(text)

    # Фоновая задача ожидания оплаты
    asyncio.create_task(_wait_for_payment_and_activate(user_id, chat_id, invoice_id, tariff_key))


async def _wait_for_payment_and_activate(
    user_id: int,
    chat_id: int,
    invoice_id: int,
    tariff_key: str,
    check_interval: int = 20,
    max_checks: int = 24,  # ~8 минут
) -> None:
    """
    Периодически опрашивает CryptoBot по invoice_id,
    при статусе paid включает premium и пишет пользователю.
    """
    from aiogram import Bot as AiogramBot  # локальный импорт, чтобы не ломать типизацию

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

            text += "Можешь не стесняться и загружать меня по полной 😉"

            await bot.send_message(chat_id, text)
            return

        if status in {"expired", "cancelled"}:
            storage.update_invoice_status(user_id, invoice_id, status)
            await bot.send_message(
                chat_id,
                f"Статус счёта: <b>{status}</b>.\n"
                "Если оплату не успел — просто оформи новый тариф из раздела «Подписка».",
            )
            return

        await asyncio.sleep(check_interval)

    await bot.send_message(
        chat_id,
        "❓ За отведённое время я не увидел подтверждения оплаты от CryptoBot.\n"
        "Если ты уверен, что оплатил — напиши админу или повторно оформи подписку из раздела «Подписка».",
    )


@router.message(Command("premium_1m"))
async def cmd_premium_1m(message: Message) -> None:
    await _create_invoice_and_wait(message, "premium_1m")


@router.message(Command("premium_3m"))
async def cmd_premium_3m(message: Message) -> None:
    await _create_invoice_and_wait(message, "premium_3m")


@router.message(Command("premium_12m"))
async def cmd_premium_12m(message: Message) -> None:
    await _create_invoice_and_wait(message, "premium_12m")


# =========================
#   Рефералы
# =========================

@router.message(F.text == BTN_REFERRALS)
async def handle_referrals(message: Message) -> None:
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
        "Поделись этим ботом с людьми, которым он реально нужен.\n\n"
        f"Твоя ссылка:\n{ref_link}\n\n"
        f"Приглашено: <b>{info['invited_count']}</b>\n"
        f"Базовый лимит: <b>{info['base_limit']}</b>\n"
        f"Бонус от рефералов: <b>{info['ref_bonus']}</b>\n"
        f"Итого лимит в день: <b>{info['limit_today']}</b>\n"
    )

    await message.answer(text)


# =========================
#   Основной диалог (стрим)
# =========================

@router.message(
    F.text
    & ~F.text.in_(TASKBAR_BUTTONS)
    & ~F.text.startswith("/")
)
async def handle_chat(message: Message) -> None:
    """
    Любой текст, который не является командой и не совпадает с
    кнопками таскбара — уходит в ядро ИИ.
    """
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
        await message.answer(text, reply_markup=build_main_keyboard())
        return

    mode_key = storage.get_mode(user_id)
    history = storage.get_history(user_id)

    # Обновляем досье и usage
    storage.append_history(user_id, "user", prompt)
    storage.update_dossier_on_message(user_id, mode_key, prompt)
    storage.increment_usage(user_id)

    bot = message.bot
    sent = await message.answer("🧠 Генерирую ответ...")

    reply_text = ""
    last_edit = datetime.now()

    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        async for chunk in ask_llm_stream(mode_key, prompt, history):
            reply_text += chunk
            now = datetime.now()
            # чтобы анимация набора была плавной, но не спамила Telegram
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
        "чтобы включить для себя админ-режим без лимитов."
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
        reply_markup=build_main_keyboard(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "<b>Кратко по навигации</b>\n\n"
        f"{BTN_MODES} — переключение режимов (команды /mode_…)\n"
        f"{BTN_PROFILE} — тариф, лимиты и память\n"
        f"{BTN_SUBSCRIPTION} — Premium через CryptoBot\n"
        f"{BTN_REFERRALS} — реферальная ссылка и бонусы к лимиту\n\n"
        "Дальше всё просто: ты пишешь запрос — я отвечаю, как будто это нативный iOS-ассистент, "
        "а не бот с кучей кнопок."
    )
    await message.answer(text)


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

    log.info("Starting Black Box bot polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
