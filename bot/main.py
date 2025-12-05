import asyncio
import logging
from datetime import datetime
from typing import List

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.filters.command import CommandObject
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
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
#   Кнопки нижнего таскбара
# =========================

BTN_MODES = "💬 Режимы"
BTN_PROFILE = "📊 Профиль"
BTN_SUBSCRIPTION = "💎 Подписка"
BTN_REFERRALS = "👥 Рефералы"
BTN_MEMORY = "🧠 Память"

TASKBAR_BUTTONS = {
    BTN_MODES,
    BTN_PROFILE,
    BTN_SUBSCRIPTION,
    BTN_REFERRALS,
    BTN_MEMORY,
}


def build_main_keyboard() -> ReplyKeyboardMarkup:
    """
    Нижний таскбар: минимализм, только ядро.
    """
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=BTN_MODES),
                KeyboardButton(text=BTN_PROFILE),
                KeyboardButton(text=BTN_SUBSCRIPTION),
            ],
            [
                KeyboardButton(text=BTN_REFERRALS),
                KeyboardButton(text=BTN_MEMORY),
            ],
        ],
        resize_keyboard=True,
        input_field_placeholder="Напиши запрос или выбери раздел ↓",
        one_time_keyboard=False,
        is_persistent=True,
    )
    return kb


def build_modes_keyboard(current_mode_key: str) -> InlineKeyboardMarkup:
    """
    Инлайн-клавиатура с режимами (вместо отдельного раздела «Сценарии»).
    """
    rows: List[InlineKeyboardButton] | List[List[InlineKeyboardButton]] = []

    rows_list: List[List[InlineKeyboardButton]] = []
    mode_items = list(ASSISTANT_MODES.items())
    row: List[InlineKeyboardButton] = []
    for key, cfg in mode_items:
        emoji = cfg.get("emoji", "")
        title = cfg.get("title", key)
        is_active = key == current_mode_key
        text = f"{emoji} {title}"
        if is_active:
            text = f"✅ {emoji} {title}"
        row.append(
            InlineKeyboardButton(
                text=text,
                callback_data=f"mode:{key}",
            )
        )
        if len(row) == 2:
            rows_list.append(row)
            row = []
    if row:
        rows_list.append(row)

    return InlineKeyboardMarkup(inline_keyboard=rows_list)


def build_subscription_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура с тарифами Premium. Нажимаешь — сразу создаётся счёт в CryptoBot.
    """
    rows: List[List[InlineKeyboardButton]] = []

    # порядок: 1м, 3м, 12м
    order = ["premium_1m", "premium_3m", "premium_12m"]
    for key in order:
        tariff = SUBSCRIPTION_TARIFFS.get(key)
        if not tariff:
            continue
        title = tariff["title"]
        amount = tariff["amount"]
        text = f"{title} — {amount:.2f} USDT"
        rows.append(
            [
                InlineKeyboardButton(
                    text=text,
                    callback_data=f"sub_tariff:{key}",
                )
            ]
        )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_payment_check_keyboard(invoice_id: int, pay_url: str) -> InlineKeyboardMarkup:
    """
    После создания счёта: кнопка оплаты + кнопка проверки оплаты.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💳 Оплатить через CryptoBot",
                    url=pay_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ Я оплатил — проверить",
                    callback_data=f"check_pay:{invoice_id}",
                )
            ],
        ]
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

    # Реферальный код в /start <code>
    if command.args:
        # Регистрируем, но не мешаем запуску
        storage.register_referral(user_id, command.args)

    user_data, is_new = storage.get_or_create_user(user_id)
    limits = storage.get_limits(user_id)
    mode_key = storage.get_mode(user_id)
    mode_cfg = ASSISTANT_MODES.get(mode_key, ASSISTANT_MODES[DEFAULT_MODE_KEY])
    mode_label = f"{mode_cfg.get('emoji', '')} {mode_cfg.get('title', mode_key)}".strip()

    if is_new or not user_data.get("onboarding_seen"):
        storage.mark_onboarding_seen(user_id)
        text = (
            "Привет, я <b>Black Box</b> — твой персональный ИИ-ассистент.\n\n"
            "Что я могу для тебя сделать:\n"
            "• 🧠 Универсальные ответы по любым темам\n"
            "• 🩺 Осторожная и структурированная медицина\n"
            "• 🔥 Личный наставник: дисциплина, режим, психология\n"
            "• 💼 Бизнес, Telegram, монетизация, стратегии\n"
            "• 🎨 Креатив: посты, названия, визуальные концепты\n\n"
            "👇 Шаг 1: выбери режим в меню «Режимы».\n"
            "👇 Шаг 2: напиши свой первый запрос.\n\n"
            "Остальное я возьму на себя."
        )
    else:
        text = (
            f"Снова на связи, {user.first_name}!\n\n"
            f"Текущий режим: <b>{mode_label}</b>\n"
            f"Сегодняшний лимит: <b>{limits['used_today']} / {limits['limit_today']}</b> запросов.\n\n"
            "Пиши запрос или используй таскбар снизу — всё управление там."
        )

    await message.answer(text, reply_markup=build_main_keyboard())


# =========================
#   Режимы
# =========================

@router.message(F.text == BTN_MODES)
async def handle_modes_menu(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    user_id = user.id

    mode_key = storage.get_mode(user_id)
    mode_cfg = ASSISTANT_MODES.get(mode_key, ASSISTANT_MODES[DEFAULT_MODE_KEY])
    mode_label = f"{mode_cfg.get('emoji', '')} {mode_cfg.get('title', mode_key)}".strip()

    text = (
        f"Текущий режим: <b>{mode_label}</b>\n\n"
        "Выбери, как я должен мыслить прямо сейчас.\n"
        "Все быстрые сценарии и логика работы завязаны на выбранный режим."
    )

    await message.answer(text, reply_markup=build_modes_keyboard(mode_key))


@router.callback_query(F.data.startswith("mode:"))
async def handle_mode_switch(cb: CallbackQuery) -> None:
    user = cb.from_user
    if not user:
        return
    user_id = user.id

    _, mode_key = cb.data.split(":", 1)
    if mode_key not in ASSISTANT_MODES:
        await cb.answer("Неизвестный режим", show_alert=True)
        return

    storage.update_mode(user_id, mode_key)
    mode_cfg = ASSISTANT_MODES[mode_key]
    mode_label = f"{mode_cfg.get('emoji', '')} {mode_cfg.get('title', mode_key)}".strip()

    examples = {
        "universal": [
            "Объясни сложную тему простыми словами",
            "Собери конспект из этого текста",
        ],
        "med": [
            "Разбери симптомы и расскажи возможные причины (без диагноза)",
            "Объясни анализы простым языком",
        ],
        "mentor": [
            "Помоги выстроить режим дня",
            "Разбери мою текущую ситуацию и дай план",
        ],
        "business": [
            "Проанализируй идею и подскажи, как запустить",
            "Сделай контент-план для Telegram-канала",
        ],
        "creative": [
            "Придумай 10 вариантов названия",
            "Сгенерируй идеи визуалов для поста",
        ],
    }
    ex_list = examples.get(mode_key, [])
    ex_text = ""
    if ex_list:
        ex_bullets = "\n".join(f"• {ex}" for ex in ex_list)
        ex_text = f"\n\nПримеры запросов в этом режиме:\n{ex_bullets}"

    text = f"Режим переключён на <b>{mode_label}</b>.{ex_text}"

    await cb.message.edit_text(text, reply_markup=build_modes_keyboard(mode_key))
    await cb.answer("Режим обновлён")


# =========================
#   Профиль
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

    text = (
        f"<b>Твой профиль</b>\n\n"
        f"{plan_line}\n"
        f"Режим: <b>{mode_label}</b>\n\n"
        f"Сегодняшний лимит: <b>{limits['used_today']} / {limits['limit_today']}</b>\n"
        f"За всё время запросов: <b>{limits['total_requests']}</b>\n"
    )

    await message.answer(text)


# =========================
#   Подписка / CryptoBot
# =========================

@router.message(F.text == BTN_SUBSCRIPTION)
async def handle_subscription(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    user_id = user.id

    plan_info = storage.get_plan_info(user_id)
    limits = storage.get_limits(user_id)

    if not CRYPTO_PAY_API_TOKEN:
        text = (
            "Раздел <b>Подписка</b>\n\n"
            "Сейчас оплата через CryptoBot ещё не настроена. "
            "Добавь CRYPTO_PAY_API_TOKEN в .env и перезапусти бота."
        )
        await message.answer(text)
        return

    plan_title = plan_info["plan_title"]
    expires = plan_info["plan_expires_at"]

    if plan_info["plan"] == "free":
        current_line = f"Текущий тариф: <b>{plan_title}</b>\n"
    else:
        if expires:
            current_line = f"Текущий тариф: <b>{plan_title}</b>, активен до <b>{expires}</b>\n"
        else:
            current_line = f"Текущий тариф: <b>{plan_title}</b>\n"

    text = (
        "<b>Premium-подписка</b>\n\n"
        f"{current_line}"
        f"Твой лимит сегодня: <b>{limits['used_today']} / {limits['limit_today']}</b> запросов.\n\n"
        "Выбери тариф ниже — я создам счёт в <b>CryptoBot</b> в USDT. "
        "После оплаты нажми кнопку «Я оплатил — проверить», и подписка активируется автоматически."
    )

    await message.answer(text, reply_markup=build_subscription_keyboard())


@router.callback_query(F.data.startswith("sub_tariff:"))
async def handle_sub_tariff(cb: CallbackQuery) -> None:
    user = cb.from_user
    if not user:
        return
    user_id = user.id

    _, tariff_key = cb.data.split(":", 1)
    tariff = SUBSCRIPTION_TARIFFS.get(tariff_key)
    if not tariff:
        await cb.answer("Тариф временно недоступен", show_alert=True)
        return

    if not CRYPTO_PAY_API_TOKEN:
        await cb.answer("Оплата через CryptoBot не настроена", show_alert=True)
        return

    try:
        invoice_id, pay_url = await create_cryptobot_invoice(user_id, tariff_key)
    except Exception as e:  # noqa: BLE001
        log.exception("Failed to create CryptoBot invoice: %s", e)
        await cb.answer("Не удалось создать счёт. Попробуй ещё раз позже.", show_alert=True)
        return

    # сохраняем в сторедж
    storage.register_invoice(
        user_id=user_id,
        invoice_id=invoice_id,
        plan="premium",
        duration_days=tariff["duration_days"],
    )

    text = (
        f"Счёт создан: <b>{tariff['title']}</b> за <b>{tariff['amount']:.2f} USDT</b>.\n\n"
        "1) Нажми кнопку «Оплатить через CryptoBot».\n"
        "2) После успешной оплаты вернись сюда и нажми «Я оплатил — проверить».\n\n"
        "Если что-то пойдёт не так — просто напиши мне в чат."
    )

    await cb.message.answer(
        text,
        reply_markup=build_payment_check_keyboard(invoice_id, pay_url),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("check_pay:"))
async def handle_check_pay(cb: CallbackQuery) -> None:
    user = cb.from_user
    if not user:
        return
    user_id = user.id

    _, invoice_str = cb.data.split(":", 1)
    try:
        invoice_id = int(invoice_str)
    except ValueError:
        await cb.answer("Некорректный идентификатор счёта", show_alert=True)
        return

    # ищем счёт в нашем хранилище
    target_user_id = None
    invoice_data = None
    for uid, inv in storage.iter_invoices(statuses=("active", "paid")):
        if inv.get("invoice_id") == invoice_id:
            target_user_id = uid
            invoice_data = inv
            break

    if not invoice_data:
        await cb.answer("Счёт не найден в базе бота", show_alert=True)
        return

    if target_user_id != user_id:
        await cb.answer("Этот счёт привязан к другому пользователю", show_alert=True)
        return

    try:
        status = await get_invoice_status(invoice_id)
    except Exception as e:  # noqa: BLE001
        log.exception("Failed to check CryptoBot invoice: %s", e)
        await cb.answer("Не удалось проверить оплату. Попробуй ещё раз позже.", show_alert=True)
        return

    if status == "paid":
        # активируем, если ещё не активировали
        storage.update_invoice_status(user_id, invoice_id, "paid")
        storage.set_plan(user_id, invoice_data.get("plan", "premium"), invoice_data.get("duration_days", 30))
        plan_info = storage.get_plan_info(user_id)
        expires = plan_info.get("plan_expires_at")

        text = (
            "Оплата найдена ✅\n\n"
            f"Тариф: <b>{plan_info['plan_title']}</b> активирован.\n"
        )
        if expires:
            text += f"Подписка действует до: <b>{expires}</b>.\n\n"
        else:
            text += "\n"

        text += "Теперь можно спокойно жарить запросы без страха за лимиты 😉"

        await cb.message.answer(text)
        await cb.answer("Подписка активирована ✅", show_alert=True)
    elif status == "active":
        await cb.answer("Пока нет информации об оплате. Если ты уже оплатил, подожди минуту и нажми ещё раз.", show_alert=True)
    elif status in {"expired", "cancelled"}:
        storage.update_invoice_status(user_id, invoice_id, status)
        await cb.answer(f"Статус счёта: {status}. Попробуй создать новый тариф.", show_alert=True)
    else:
        await cb.answer(f"Текущий статус счёта: {status}", show_alert=True)


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
        link_text = f'<a href="{ref_link}">{ref_link}</a>'
    else:
        ref_link = info["code"]
        link_text = ref_link

    text = (
        "<b>Реферальная программа</b>\n\n"
        "Поделись ботом с друзьями и коллегами.\n"
        "За каждого активного реферала я увеличу твой дневной лимит.\n\n"
        f"Твоя персональная ссылка:\n{link_text}\n\n"
        f"Приглашено людей: <b>{info['invited_count']}</b>\n"
        f"Базовый лимит: <b>{info['base_limit']}</b>\n"
        f"Бонус с рефералов: <b>+{info['ref_bonus']}</b>\n"
        f"Итого лимит в день: <b>{info['limit_today']}</b>\n"
    )

    await message.answer(text)


# =========================
#   Память / досье
# =========================

@router.message(F.text == BTN_MEMORY)
async def handle_memory(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    user_id = user.id

    preview = storage.get_dossier_preview(user_id)
    text = (
        "<b>Память и досье</b>\n\n"
        "Я постепенно запоминаю твой стиль и темы запросов.\n"
        "Пока что это простая версия досье, дальше можно будет прокачать.\n\n"
        f"{preview}"
    )
    await message.answer(text)


# =========================
#   Основной чат (стрим)
# =========================

@router.message(
    F.text
    & ~F.text.in_(TASKBAR_BUTTONS)
    & ~F.text.startswith("/")
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
            f"Сделано: <b>{limits['used_today']} / {limits['limit_today']}</b>.\n"
            "Можно подождать до завтра или оформить Premium в разделе «Подписка»."
        )
        await message.answer(text)
        return

    mode_key = storage.get_mode(user_id)
    history = storage.get_history(user_id)

    # Обновляем досье и usage
    storage.append_history(user_id, "user", prompt)
    storage.update_dossier_on_message(user_id, mode_key, prompt)
    storage.increment_usage(user_id)

    bot = message.bot

    sent = await message.answer("🧠 Генерирую ответ...", parse_mode=ParseMode.HTML)

    reply_text = ""
    last_edit = datetime.now()

    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        async for chunk in ask_llm_stream(mode_key, prompt, history):
            reply_text += chunk
            # Обновляем раз в ~0.7 сек, чтобы не спамить Telegram
            now = datetime.now()
            if (now - last_edit).total_seconds() > 0.7 and reply_text:
                # режем до 4096 символов, чтобы не словить ошибку Telegram
                view = reply_text[-4096:]
                await sent.edit_text(view, parse_mode=None)
                last_edit = now

    if reply_text:
        view = reply_text[-4096:]
        await sent.edit_text(view, parse_mode=None)
    else:
        await sent.edit_text(
            "Не получилось получить ответ от модели. Попробуй ещё раз.",
            parse_mode=ParseMode.HTML,
        )

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


# =========================
#   /help и прочие команды
# =========================

@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "<b>Как со мной работать</b>\n\n"
        "1) Выбери режим в таскбаре: «Режимы».\n"
        "2) Просто формулируй задачу человеческим языком.\n"
        "3) Если лимит закончился — загляни в «Подписка».\n\n"
        "Ключевая идея: минимум кнопок, максимум мощности ядра. "
        "Пиши, а я уже сам подстроюсь под контекст."
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
