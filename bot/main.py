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
    LabeledPrice,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
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

# Подписи для кнопок нижнего таскбара
MODE_BUTTON_LABELS = {
    "universal": "🧠 Универсальный",
    "med": "⚕️ Медицина",
    "coach": "🔥 Наставник",
    "biz": "💼 Бизнес / Идеи",
    "creative": "🎨 Креатив",
}

SERVICE_BUTTON_LABELS = {
    "templates": "⚡ Сценарии",
    "profile": "👤 Профиль",
    "referral": "🎁 Реферал",
    "plans": "💳 Тарифы",
}

BUY_BUTTON_PRO = "Pro ⭐"
BUY_BUTTON_VIP = "VIP 💎"

# Все тексты кнопок, чтобы не отправлять их в LLM
ALL_BUTTON_TEXTS = (
    list(MODE_BUTTON_LABELS.values())
    + list(SERVICE_BUTTON_LABELS.values())
    + [BUY_BUTTON_PRO, BUY_BUTTON_VIP]
)


def get_user_state(user_id: int) -> UserState:
    """
    Берём состояние пользователя из памяти + синхронизируем с файловым хранилищем.
    """
    if user_id not in user_states:
        stored = storage.get_or_create_user(user_id)
        mode_key = stored.get("mode_key", DEFAULT_MODE_KEY)
        user_states[user_id] = UserState(mode_key=mode_key)
    return user_states[user_id]


# =========================
#  Нижний таскбар (ReplyKeyboard)
# =========================


def build_main_keyboard() -> ReplyKeyboardMarkup:
    """
    Постоянная клавиатура внизу. Никаких inline-кнопок в сообщениях.
    """
    rows = [
        [
            KeyboardButton(text=MODE_BUTTON_LABELS["universal"]),
            KeyboardButton(text=MODE_BUTTON_LABELS["med"]),
        ],
        [
            KeyboardButton(text=MODE_BUTTON_LABELS["coach"]),
            KeyboardButton(text=MODE_BUTTON_LABELS["biz"]),
        ],
        [KeyboardButton(text=MODE_BUTTON_LABELS["creative"])],
        [
            KeyboardButton(text=SERVICE_BUTTON_LABELS["templates"]),
            KeyboardButton(text=SERVICE_BUTTON_LABELS["profile"]),
        ],
        [
            KeyboardButton(text=SERVICE_BUTTON_LABELS["referral"]),
            KeyboardButton(text=SERVICE_BUTTON_LABELS["plans"]),
        ],
    ]

    # Кнопки покупки тарифов тоже в таскбаре
    if PAYMENTS_ENABLED:
        rows.append(
            [
                KeyboardButton(text=BUY_BUTTON_PRO),
                KeyboardButton(text=BUY_BUTTON_VIP),
            ]
        )

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Напиши запрос...",
    )


# =========================
#  Router и вспомогалки
# =========================

router = Router()


def _ref_level(invited_count: int) -> str:
    if invited_count >= 20:
        return "Амбассадор"
    if invited_count >= 5:
        return "Партнёр"
    if invited_count >= 1:
        return "Новичок"
    return "—"


# =========================
#  Старт, профиль, тарифы
# =========================


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject) -> None:
    user_id = message.from_user.id
    state = get_user_state(user_id)

    # Реферальный код из /start
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
        "Управление — только нижний таскбар. Просто пиши запрос.\n\n"
        f"Текущий режим: <b>{mode_cfg['title']}</b>\n"
        f"<i>{mode_cfg['description']}</i>\n\n"
        f"Тариф: <b>{limits['plan_title']}</b>\n"
        f"Лимит на сегодня: <b>{limits['used_today']}/{limits['limit_today']}</b> запросов."
        f"{ref_msg}"
    )

    await message.answer(
        text,
        reply_markup=build_main_keyboard(),
    )


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
    Сброс диалогового контекста.
    """
    user_id = message.from_user.id
    storage.reset_history(user_id)
    state = get_user_state(user_id)
    state.last_answer = None
    state.last_prompt = None

    await message.answer("🔄 Диалоговый контекст сброшен. Можем начать с чистого листа.")


@router.message(Command("plans"))
async def cmd_plans(message: Message) -> None:
    """
    Обзор тарифов текстом. Покупка — кнопками Pro/VIP в таскбаре.
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
    if PAYMENTS_ENABLED:
        lines.append("Оформить Pro/VIP можно кнопками Pro ⭐ / VIP 💎 в нижнем таскбаре.")

    await message.answer("\n".join(lines))


# =========================
#  Кнопки режимов (нижний таскбар)
# =========================


@router.message(F.text.in_(list(MODE_BUTTON_LABELS.values())))
async def mode_button(message: Message) -> None:
    user_id = message.from_user.id
    state = get_user_state(user_id)
    label = message.text

    mode_key = None
    for k, v in MODE_BUTTON_LABELS.items():
        if v == label:
            mode_key = k
            break
    if mode_key is None or mode_key not in ASSISTANT_MODES:
        await message.answer("Неизвестный режим.")
        return

    state.mode_key = mode_key
    storage.update_user_mode(user_id, mode_key)

    mode_cfg = ASSISTANT_MODES[mode_key]
    limits = storage.get_limits(user_id)

    text = (
        "Режим обновлён ✅\n\n"
        f"Текущий режим: <b>{mode_cfg['title']}</b>\n"
        f"<i>{mode_cfg['description']}</i>\n\n"
        f"Тариф: <b>{limits['plan_title']}</b>\n"
        f"Лимит на сегодня: <b>{limits['used_today']}/{limits['limit_today']}</b>."
    )

    await message.answer(text)


# =========================
#  Кнопки сервисов (нижний таскбар)
# =========================


@router.message(F.text.in_(list(SERVICE_BUTTON_LABELS.values())))
async def service_button(message: Message) -> None:
    user_id = message.from_user.id
    state = get_user_state(user_id)
    label = message.text

    action = None
    for k, v in SERVICE_BUTTON_LABELS.items():
        if v == label:
            action = k
            break

    if action is None:
        await message.answer("Сервис в разработке.")
        return

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
        code = storage.ensure_ref_code(user_id)
        stats = storage.get_referral_stats(user_id)
        level = _ref_level(stats["invited_count"])

        me = await message.bot.get_me()
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
        if PAYMENTS_ENABLED:
            lines.append("Оформить Pro/VIP можно кнопками Pro ⭐ / VIP 💎 в нижнем таскбаре.")
        text = "\n".join(lines)
    else:
        text = "Сервис в разработке."

    await message.answer(text)


# =========================
#  Покупка тарифов (кнопки Pro/VIP в таскбаре)
# =========================


@router.message(F.text.in_([BUY_BUTTON_PRO, BUY_BUTTON_VIP]))
async def buy_button(message: Message, bot: Bot) -> None:
    if not PAYMENTS_ENABLED:
        await message.answer(
            "Платежи пока не настроены. Свяжись с админом или попробуй позже.",
        )
        return

    label = message.text
    plan = "pro" if label == BUY_BUTTON_PRO else "vip"

    if plan not in PLAN_PRICES:
        await message.answer("Цена для этого тарифа не настроена.")
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
        chat_id=message.chat.id,
        title=title,
        description=description,
        payload=payload,
        provider_token=PAYMENT_PROVIDER_TOKEN,
        currency=PAYMENT_CURRENCY,
        prices=prices,
        start_parameter=f"buy_{plan}",
    )


@router.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery, bot: Bot) -> None:
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
#  Главный LLM-handler
# =========================


@router.message(F.text & ~F.via_bot & ~F.text.in_(ALL_BUTTON_TEXTS))
async def handle_text(message: Message) -> None:
    """
    Любой обычный текст (не кнопка, не команда) — уходит в LLM.
    """
    user_id = message.from_user.id
    text = message.text or ""

    if text.startswith("/"):
        return

    state = get_user_state(user_id)
    mode_cfg = ASSISTANT_MODES.get(state.mode_key, ASSISTANT_MODES[DEFAULT_MODE_KEY])

    # Обновляем досье
    storage.update_dossier_on_message(user_id, state.mode_key, text)

    # Лимиты
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

    # typing…
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    waiting_message = await message.answer(
        "⌛ Обрабатываю запрос в режиме "
        f"<b>{mode_cfg['title']}</b>...\n\nГенерация идёт в реальном времени.",
    )

    user_prompt = text.strip()
    state.last_prompt = user_prompt

    storage.register_request(user_id)

    history = storage.get_history(user_id)

    answer_text = ""
    chunk_counter = 0
    EDIT_EVERY_N_CHUNKS = 3

    try:
        async for chunk in ask_llm_stream(state.mode_key, user_prompt, history):
            answer_text += chunk
            chunk_counter += 1

            if chunk_counter % 5 == 0:
                try:
                    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
                except Exception:
                    pass

            if chunk_counter % EDIT_EVERY_N_CHUNKS == 0:
                try:
                    await waiting_message.edit_text(answer_text)
                except Exception:
                    pass

        if not answer_text.strip():
            answer_text = (
                "Что-то пошло не так при генерации ответа. Попробуй сформулировать запрос по-другому."
            )

        state.last_answer = answer_text

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
