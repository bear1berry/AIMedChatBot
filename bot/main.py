import asyncio
import logging
from typing import Dict, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatAction
from aiogram.filters import CommandStart, Command
from aiogram.filters.command import CommandObject
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    PreCheckoutQuery,
)

from bot.config import (
    BOT_TOKEN,
    ASSISTANT_MODES,
    DEFAULT_MODE_KEY,
    PLAN_LIMITS,
    REF_BONUS_PER_USER,
    PAYMENT_PROVIDER_TOKEN,
    PAYMENT_CURRENCY,
    PLAN_PRICES,
    PAYMENTS_ENABLED,
)
from services.llm import ask_llm_stream
from services.storage import Storage

# =========================
#  Глобальное хранилище
# =========================

storage = Storage()  # data/users.json

# =========================
#  In-memory состояние
# =========================


class UserState:
    def __init__(self, mode_key: str = DEFAULT_MODE_KEY) -> None:
        self.mode_key = mode_key
        self.last_prompt: Optional[str] = None
        self.last_answer: Optional[str] = None


user_states: Dict[int, UserState] = {}

# Сообщение-панель с таскбаром на чат (по chat_id)
panel_messages: Dict[int, int] = {}


def get_user_state(user_id: int) -> UserState:
    """
    Достаём состояние из памяти и синхронизируем с файловым хранилищем.
    """
    if user_id not in user_states:
        stored = storage.get_or_create_user(user_id)
        mode_key = stored.get("mode_key", DEFAULT_MODE_KEY)
        user_states[user_id] = UserState(mode_key=mode_key)
    return user_states[user_id]


# =========================
#  Клавиатура (нижний таскбар)
# =========================


def build_main_keyboard(active_mode_key: str) -> InlineKeyboardMarkup:
    """
    Нижний таскбар: режимы ассистента + сервисные кнопки + (опционально) Pro/VIP.
    Все интерактивные действия — только здесь.
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
        InlineKeyboardButton(text="💳 Тарифы", callback_data="service:plans"),
    ]

    rows = [mode_buttons, service_buttons]

    # Кнопки покупки тарифов тоже часть нижнего таскбара
    if PAYMENTS_ENABLED:
        buy_buttons = [
            InlineKeyboardButton(text="Pro ⭐", callback_data="buy:pro"),
            InlineKeyboardButton(text="VIP 💎", callback_data="buy:vip"),
        ]
        rows.append(buy_buttons)

    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    return keyboard


# =========================
#  Router
# =========================

router = Router()


# =========================
#  Вспомогательные функции
# =========================


def _ref_level(invited_count: int) -> str:
    if invited_count >= 20:
        return "Амбассадор"
    if invited_count >= 5:
        return "Партнёр"
    if invited_count >= 1:
        return "Новичок"
    return "—"


def _plan_description(plan: str) -> str:
    cfg = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])
    return cfg.get("description", "")


# =========================
#  Handlers: старт, профиль, режимы
# =========================


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject) -> None:
    user_id = message.from_user.id
    chat_id = message.chat.id
    state = get_user_state(user_id)

    # Обработка реферального кода из /start
    ref_msg = ""
    ref_code_raw = (command.args or "").strip() if command else ""
    if ref_code_raw:
        arg = ref_code_raw.strip()
        if arg.lower().startswith("ref_"):
            arg = arg[4:]
        arg = arg.upper()

        status = storage.attach_referral(user_id, arg)
        if status == "ok":
            ref_msg = (
                "\n\n🎁 Твой аккаунт привязан к реферальному коду. "
                "Ты получил бонусные дневные лимиты."
            )
        elif status == "not_found":
            ref_msg = "\n\n⚠️ Реферальный код не найден, но бот всё равно доступен."
        elif status == "already_has_referrer":
            ref_msg = "\n\nℹ️ Реферальный код уже был привязан ранее."
        elif status == "self_referral":
            ref_msg = "\n\n⚠️ Нельзя использовать собственный реферальный код."

    mode_cfg = ASSISTANT_MODES[state.mode_key]
    limits = storage.get_limits(user_id)

    text = (
        "🖤 <b>BlackBoxGPT</b>\n\n"
        "Твой персональный ИИ-ассистент.\n"
        "Навигация — только нижний таскбар, просто пиши запрос.\n\n"
        f"Текущий режим: <b>{mode_cfg['title']}</b>\n"
        f"<i>{mode_cfg['description']}</i>\n\n"
        f"Тариф: <b>{limits['plan_title']}</b>\n"
        f"Лимит на сегодня: <b>{limits['used_today']}/{limits['limit_today']}</b> запросов."
        f"{ref_msg}"
    )

    kb = build_main_keyboard(state.mode_key)

    # Пытаемся обновить существующую панель, а не плодить новые
    panel_msg_id = panel_messages.get(chat_id)
    if panel_msg_id:
        try:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=panel_msg_id,
                text=text,
                reply_markup=kb,
            )
            return
        except Exception:
            # если не удалось (сообщение удалено и т.п.) — создаём новое
            pass

    sent = await message.answer(
        text,
        reply_markup=kb,
    )
    panel_messages[chat_id] = sent.message_id


@router.message(Command("mode"))
async def cmd_mode(message: Message) -> None:
    state = get_user_state(message.from_user.id)
    text_lines = ["Выбери режим ассистента:\n"]
    for key, cfg in ASSISTANT_MODES.items():
        prefix = "•" if key == state.mode_key else "–"
        text_lines.append(f"{prefix} {cfg['title']} — {cfg['description']}")
    await message.answer("\n".join(text_lines))


@router.message(Command("profile"))
async def cmd_profile(message: Message) -> None:
    user_id = message.from_user.id
    state = get_user_state(user_id)
    user = storage.get_or_create_user(user_id)
    dossier = user.get("dossier", {})
    stats = storage.get_referral_stats(user_id)

    mode_cfg = ASSISTANT_MODES.get(state.mode_key, ASSISTANT_MODES[DEFAULT_MODE_KEY])
    level = _ref_level(stats["invited_count"])

    text = (
        "👤 <b>Твой профиль</b>\n\n"
        f"<b>Режим по умолчанию:</b> {mode_cfg['title']}\n"
        f"<b>Сообщений всего:</b> {dossier.get('messages_count', 0)}\n"
        f"<b>Последний запрос:</b> <i>{dossier.get('last_prompt_preview', '')}</i>\n\n"
        "💳 <b>Тариф</b>\n"
        f"Текущий тариф: <b>{stats['plan_title']}</b>\n"
        f"Лимит на сегодня: <b>{stats['used_today']}/{stats['limit_today']}</b> запросов\n"
        f"Базовый лимит: <b>{stats['base_limit']}</b>\n"
        f"Бонус от рефералов: <b>{stats['ref_bonus']} (по {REF_BONUS_PER_USER} за каждого)</b>\n"
        f"Всего запросов за всё время: <b>{stats['total_requests']}</b>\n\n"
        "🎁 <b>Реферальная система</b>\n"
        f"Твой код: <code>{stats['code'] or 'ещё не сгенерирован'}</code>\n"
        f"Приглашено: <b>{stats['invited_count']}</b> (уровень: <b>{level}</b>)\n"
    )

    await message.answer(text)


@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    """
    Сбрасывает диалоговый контекст (history) для пользователя.
    """
    user_id = message.from_user.id
    storage.reset_history(user_id)
    state = get_user_state(user_id)
    state.last_answer = None
    state.last_prompt = None

    await message.answer(
        "🔄 Диалоговый контекст сброшен. Можем начать с чистого листа.",
    )


@router.message(Command("plans"))
async def cmd_plans(message: Message) -> None:
    """
    Обзор тарифов (только текст).
    Покупка — через нижний таскбар (кнопки Pro/VIP).
    """
    user_id = message.from_user.id
    limits = storage.get_limits(user_id)

    lines = [
        "💳 <b>Тарифы BlackBoxGPT</b>\n",
        f"Твой текущий тариф: <b>{limits['plan_title']}</b>",
        f"Лимит на сегодня: <b>{limits['used_today']}/{limits['limit_today']}</b> запросов.\n",
    ]
    for key, cfg in PLAN_LIMITS.items():
        lines.append(
            f"• <b>{cfg['title']}</b> ({key}) — до <b>{cfg['daily_base']}</b> запросов в день."
        )
        lines.append(f"  {cfg.get('description', '')}\n")

    lines.append(
        f"За каждого приглашённого друга ты получаешь +<b>{REF_BONUS_PER_USER}</b> "
        "запросов в день к своему тарифу.\n"
    )
    lines.append("Купить Pro или VIP можно через нижний таскбар.")

    await message.answer("\n".join(lines))


# =========================
#  Handlers: смена режима, сервисные панели
# =========================


@router.callback_query(F.data.startswith("mode:"))
async def cb_change_mode(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    state = get_user_state(user_id)

    _, mode_key = callback.data.split(":", 1)
    if mode_key not in ASSISTANT_MODES:
        await callback.answer("Неизвестный режим", show_alert=True)
        return

    state.mode_key = mode_key
    storage.update_user_mode(user_id, mode_key)

    mode_cfg = ASSISTANT_MODES[mode_key]
    limits = storage.get_limits(user_id)

    new_text = (
        "Режим обновлён ✅\n\n"
        f"Текущий режим: <b>{mode_cfg['title']}</b>\n"
        f"<i>{mode_cfg['description']}</i>\n\n"
        f"Тариф: <b>{limits['plan_title']}</b>\n"
        f"Лимит на сегодня: <b>{limits['used_today']}/{limits['limit_today']}</b>."
    )

    kb = build_main_keyboard(state.mode_key)

    try:
        await callback.message.edit_text(
            new_text,
            reply_markup=kb,
        )
        panel_messages[chat_id] = callback.message.message_id
    except Exception:
        sent = await callback.message.answer(new_text, reply_markup=kb)
        panel_messages[chat_id] = sent.message_id

    await callback.answer("Режим переключен")


@router.callback_query(F.data.startswith("service:"))
async def cb_service(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
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
        user = storage.get_or_create_user(user_id)
        dossier = user.get("dossier", {})
        stats = storage.get_referral_stats(user_id)
        mode_cfg = ASSISTANT_MODES.get(state.mode_key, ASSISTANT_MODES[DEFAULT_MODE_KEY])
        level = _ref_level(stats["invited_count"])

        text = (
            "👤 <b>Твой профиль</b>\n\n"
            f"<b>Режим по умолчанию:</b> {mode_cfg['title']}\n"
            f"<b>Сообщений всего:</b> {dossier.get('messages_count', 0)}\n"
            f"<b>Последний запрос:</b> <i>{dossier.get('last_prompt_preview', '')}</i>\n\n"
            "💳 <b>Тариф</b>\n"
            f"Текущий тариф: <b>{stats['plan_title']}</b>\n"
            f"Лимит на сегодня: <b>{stats['used_today']}/{stats['limit_today']}</b> запросов\n"
            f"Базовый лимит: <b>{stats['base_limit']}</b>\n"
            f"Бонус от рефералов: <b>{stats['ref_bonus']} (по {REF_BONUS_PER_USER} за каждого)</b>\n"
            f"Всего запросов за всё время: <b>{stats['total_requests']}</b>\n\n"
            "🎁 <b>Реферальная система</b>\n"
            f"Твой код: <code>{stats['code'] or 'ещё не сгенерирован'}</code>\n"
            f"Приглашено: <b>{stats['invited_count']}</b> (уровень: <b>{level}</b>)\n"
        )
    elif action == "referral":
        # Генерация и показ реферальной ссылки
        code = storage.ensure_ref_code(user_id)
        stats = storage.get_referral_stats(user_id)
        level = _ref_level(stats["invited_count"])

        me = await callback.message.bot.get_me()
        username = me.username or "YourBot"
        link = f"https://t.me/{username}?start=ref_{code}"

        text = (
            "🎁 <b>Твоя реферальная программа</b>\n\n"
            f"Тариф: <b>{stats['plan_title']}</b>\n"
            f"Лимит на сегодня: <b>{stats['used_today']}/{stats['limit_today']}</b>\n"
            f"Базовый лимит: <b>{stats['base_limit']}</b>\n"
            f"Бонус от рефералов: <b>{stats['ref_bonus']} (по {REF_BONUS_PER_USER} за каждого)</b>\n\n"
            f"Твой код: <code>{code}</code>\n"
            f"Твоя ссылка: <code>{link}</code>\n\n"
            f"Приглашено: <b>{stats['invited_count']}</b> (уровень: <b>{level}</b>)\n\n"
            "Каждый приглашённый через твою ссылку даёт дополнительные запросы в день."
        )
    elif action == "plans":
        limits = storage.get_limits(user_id)
        lines = [
            "💳 <b>Тарифы BlackBoxGPT</b>\n",
            f"Твой текущий тариф: <b>{limits['plan_title']}</b>",
            f"Лимит на сегодня: <b>{limits['used_today']}/{limits['limit_today']}</b> запросов.\n",
        ]
        for key, cfg in PLAN_LIMITS.items():
            lines.append(
                f"• <b>{cfg['title']}</b> ({key}) — до <b>{cfg['daily_base']}</b> запросов в день."
            )
            lines.append(f"  {cfg.get('description', '')}\n")

        lines.append(
            f"За каждого приглашённого друга ты получаешь +<b>{REF_BONUS_PER_USER}</b> "
            "запросов в день к своему тарифу.\n"
        )
        lines.append("Купить Pro или VIP можно кнопками в нижнем таскбаре.")
        text = "\n".join(lines)
    elif action == "plans_info":
        text = (
            "💳 <b>Оплата</b>\n\n"
            "Платёжный провайдер пока не настроен.\n"
            "Скоро здесь появится возможность оформить Pro/VIP прямо в боте."
        )
    else:
        text = "Сервис в разработке."

    kb = build_main_keyboard(state.mode_key)

    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb,
        )
        panel_messages[chat_id] = callback.message.message_id
    except Exception:
        sent = await callback.message.answer(text, reply_markup=kb)
        panel_messages[chat_id] = sent.message_id

    await callback.answer()


# =========================
#  Handlers: оплата и апгрейд тарифа
# =========================


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id
    _, plan = callback.data.split(":", 1)

    if not PAYMENTS_ENABLED:
        await callback.answer(
            "Платежи пока не настроены. Свяжись с админом или попробуй позже.",
            show_alert=True,
        )
        return

    if plan not in ("pro", "vip"):
        await callback.answer("Этот тариф недоступен для покупки.", show_alert=True)
        return

    if plan not in PLAN_PRICES:
        await callback.answer("Цена для этого тарифа не настроена.", show_alert=True)
        return

    price_amount = PLAN_PRICES[plan]
    plan_cfg = PLAN_LIMITS.get(plan, PLAN_LIMITS["pro"])
    title = f"Тариф {plan_cfg['title']}"
    description = (
        f"{plan_cfg.get('description', '')}\n\n"
        f"Дневной базовый лимит: {plan_cfg['daily_base']} запросов.\n"
        f"Бонусы от рефералов сохраняются."
    )

    prices = [LabeledPrice(label=title, amount=price_amount)]
    payload = f"plan:{plan}"

    await bot.send_invoice(
        chat_id=callback.message.chat.id,
        title=title,
        description=description,
        payload=payload,
        provider_token=PAYMENT_PROVIDER_TOKEN,
        currency=PAYMENT_CURRENCY,
        prices=prices,
        start_parameter=f"buy_{plan}",
    )

    await callback.answer()


@router.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery, bot: Bot) -> None:
    """
    Обязательный обработчик pre_checkout_query — подтверждаем, что всё ок.
    """
    try:
        await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
    except Exception as e:  # noqa: BLE001
        logging.exception("Error in pre_checkout_query: %s", e)
        await bot.answer_pre_checkout_query(
            pre_checkout_query.id,
            ok=False,
            error_message="Произошла ошибка при обработке платежа. Попробуйте позже.",
        )


@router.message(F.successful_payment)
async def successful_payment_handler(message: Message) -> None:
    """
    Обработка успешного платежа: апгрейд тарифа.
    """
    sp = message.successful_payment
    payload = sp.invoice_payload or ""
    user_id = message.from_user.id

    plan = None
    if payload.startswith("plan:"):
        plan = payload.split(":", 1)[1]

    if plan not in PLAN_LIMITS:
        await message.answer(
            "Платёж прошёл, но тариф определить не удалось. Обратись в поддержку.",
        )
        return

    # Апгрейд плана
    storage.set_plan(user_id, plan)
    limits = storage.get_limits(user_id)
    plan_cfg = PLAN_LIMITS[plan]

    text = (
        "✅ <b>Оплата прошла успешно!</b>\n\n"
        f"Твой новый тариф: <b>{limits['plan_title']}</b>\n"
        f"Дневной базовый лимит: <b>{plan_cfg['daily_base']}</b> запросов.\n"
        f"С учётом бонусов от рефералов лимит на сегодня: "
        f"<b>{limits['used_today']}/{limits['limit_today']}</b>.\n\n"
        "Спасибо за поддержку проекта 🖤"
    )

    await message.answer(text)


# =========================
#  Handler: основной текст + LLM
# =========================


@router.message(F.text & ~F.via_bot)
async def handle_text(message: Message) -> None:
    """
    Главный обработчик любых текстовых запросов пользователя.
    Поддерживает:
      - диалоговый контекст (history)
      - стриминг ответа (по чанкам)
      - тарифы и суточные лимиты
    """
    user_id = message.from_user.id
    text = message.text or ""

    # Не обрабатываем команды здесь
    if text.startswith("/"):
        return

    state = get_user_state(user_id)
    mode_cfg = ASSISTANT_MODES.get(state.mode_key, ASSISTANT_MODES[DEFAULT_MODE_KEY])

    # Обновляем досье
    storage.update_dossier_on_message(user_id, state.mode_key, text)

    # Проверяем лимиты
    if not storage.can_make_request(user_id):
        limits = storage.get_limits(user_id)
        await message.answer(
            (
                "⚠️ Лимит запросов на сегодня исчерпан.\n\n"
                f"Тариф: <b>{limits['plan_title']}</b>\n"
                f"Сегодня использовано: <b>{limits['used_today']}/{limits['limit_today']}</b> запросов.\n\n"
                "Пригласи друзей по реферальной ссылке (кнопка «🎁 Реферал» внизу), "
                "чтобы получить дополнительные дневные лимиты.\n\n"
                "Или открой /plans и апгрейдни тариф до Pro/VIP."
            ),
        )
        return

    # Показываем typing-индикатор
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    waiting_message = await message.answer(
        "⌛ Обрабатываю запрос в режиме "
        f"<b>{mode_cfg['title']}</b>...\n\nГенерация идёт в реальном времени.",
    )

    user_prompt = text.strip()
    state.last_prompt = user_prompt

    # Регистрируем использование лимита
    storage.register_request(user_id)

    # Берём диалоговую историю для контекста
    history = storage.get_history(user_id)

    answer_text = ""
    chunk_counter = 0
    EDIT_EVERY_N_CHUNKS = 3  # апдейтим сообщение почаще для более плавного UX

    try:
        async for chunk in ask_llm_stream(state.mode_key, user_prompt, history):
            answer_text += chunk
            chunk_counter += 1

            # поддерживаем typing-индикатор
            if chunk_counter % 5 == 0:
                try:
                    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
                except Exception:
                    pass

            if chunk_counter % EDIT_EVERY_N_CHUNKS == 0:
                try:
                    await waiting_message.edit_text(answer_text)
                except Exception:
                    # Игнорим ошибки типа "message is not modified" или rate limit
                    pass

        # Стрим закончился — финальный текст
        if not answer_text.strip():
            answer_text = (
                "Что-то пошло не так при генерации ответа. Попробуй сформулировать запрос по-другому."
            )

        state.last_answer = answer_text

        # Обновляем history (user + assistant)
        storage.append_history(user_id, "user", user_prompt)
        storage.append_history(user_id, "assistant", answer_text)

        await waiting_message.edit_text(answer_text)

    except Exception as e:  # noqa: BLE001
        logging.exception("Unexpected error while handling text with streaming: %s", e)
        fallback = (
            answer_text.strip()
            if answer_text.strip()
            else "❌ Произошла неожиданная ошибка. Попробуй ещё раз позже."
        )
        await waiting_message.edit_text(fallback)


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
