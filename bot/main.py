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
    ReplyKeyboardMarkup,
    KeyboardButton,
)

from bot.config import (
    BOT_TOKEN,
    ASSISTANT_MODES,
    DEFAULT_MODE_KEY,
    PLAN_LIMITS,
    REF_BONUS_PER_USER,
    ADMIN_USER_IDS,
    CRYPTO_USDT_LINK_MONTH,
    CRYPTO_USDT_LINK_3M,
    CRYPTO_USDT_LINK_YEAR,
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


def get_user_state(user_id: int) -> UserState:
    """
    Берём состояние пользователя из памяти + синхронизируем с файловым хранилищем.
    """
    if user_id not in user_states:
        stored = storage.get_or_create_user(user_id)
        mode_key = stored.get("mode_key", DEFAULT_MODE_KEY)
        user_states[user_id] = UserState(mode_key=mode_key)
    return user_states[user_id]


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


# Подписи для кнопок (нижний таскбар)

BTN_MODES = "🧠 Режимы"
BTN_SCENARIOS = "⚡ Сценарии"
BTN_PROFILE = "👤 Профиль"
BTN_REFERRAL = "🎁 Реферал"
BTN_TARIFFS = "💳 Подписка"
BTN_BACK = "⬅️ Назад"

MODE_BUTTON_LABELS = {
    "universal": "🧠 Универсальный",
    "med": "⚕️ Медицина",
    "coach": "🔥 Наставник",
    "biz": "💼 Бизнес / Идеи",
    "creative": "🎨 Креатив",
}

SERVICE_BUTTON_LABELS = {
    "templates": BTN_SCENARIOS,
    "profile": BTN_PROFILE,
    "referral": BTN_REFERRAL,
    "plans": BTN_TARIFFS,
}

ALL_BUTTON_TEXTS = (
    [BTN_MODES, BTN_SCENARIOS, BTN_PROFILE, BTN_REFERRAL, BTN_TARIFFS, BTN_BACK]
    + list(MODE_BUTTON_LABELS.values())
    + list(SERVICE_BUTTON_LABELS.values())
)

# =========================
#  Клавиатуры (нижний таскбар)
# =========================


def build_main_keyboard() -> ReplyKeyboardMarkup:
    """
    Главное меню внизу: компактно и логично.
    """
    rows = [
        [
            KeyboardButton(text=BTN_MODES),
            KeyboardButton(text=BTN_SCENARIOS),
            KeyboardButton(text=BTN_PROFILE),
        ],
        [
            KeyboardButton(text=BTN_REFERRAL),
            KeyboardButton(text=BTN_TARIFFS),
        ],
    ]
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Напиши запрос...",
    )


def build_modes_keyboard() -> ReplyKeyboardMarkup:
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
        [KeyboardButton(text=BTN_BACK)],
    ]
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Выбери режим или вернись назад",
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
#  Старт, онбординг, профиль, тарифы
# =========================


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject) -> None:
    user_id = message.from_user.id
    user = storage.get_or_create_user(user_id)
    state = get_user_state(user_id)

    # Онбординг при первом запуске
    if not user.get("onboarding_done"):
        onboarding_text = (
            "👋 Добро пожаловать в <b>BlackBoxGPT</b>.\n\n"
            "Как со мной работать:\n"
            "1️⃣ Выбери режим в кнопке «🧠 Режимы» внизу.\n"
            "2️⃣ Напиши задачу обычным языком — от жизни до кода.\n"
            "3️⃣ Я держу контекст в рамках сессии. Если нужно начать с нуля — используй команду /reset.\n\n"
            "Внизу всего несколько кнопок: режимы, сценарии, профиль, реферал и подписка. "
            "Всё остальное — через живой текст."
        )
        await message.answer(onboarding_text, reply_markup=build_main_keyboard())
        user["onboarding_done"] = True
        # сохраняем изменения
        try:
            storage._save()  # type: ignore[attr-defined]
        except Exception:
            # если вдруг реализация другая — просто продолжаем, это не критично
            pass

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
                "\n\n🎁 Реферальный код привязан. "
                "Бонусные лимиты активированы."
            )
        elif status == "not_found":
            ref_msg = "\n\n⚠️ Реферальный код не найден."
        elif status == "already_has_referrer":
            ref_msg = "\n\nℹ️ Реферальный код уже был привязан."
        elif status == "self_referral":
            ref_msg = "\n\n⚠️ Нельзя использовать собственный код."

    mode_cfg = ASSISTANT_MODES[state.mode_key]
    limits = storage.get_limits(user_id)

    if is_admin(user_id):
        tariff_block = "Тариф: <b>Admin</b>\nЛимит на сегодня: <b>без ограничений</b>."
    else:
        tariff_block = (
            f"Тариф: <b>{limits['plan_title']}</b>\n"
            f"Лимит на сегодня: <b>{limits['used_today']}/{limits['limit_today']}</b> запросов."
        )

    text = (
        "🖤 <b>BlackBoxGPT</b>\n\n"
        "Минимум кнопок, максимум пользы.\n"
        "Выбери раздел внизу или просто напиши вопрос.\n\n"
        f"Текущий режим: <b>{mode_cfg['title']}</b>\n"
        f"<i>{mode_cfg['description']}</i>\n\n"
        f"{tariff_block}"
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

    if is_admin(user_id):
        tariff_line = "Текущий тариф: <b>Admin</b> (лимитов нет)"
    else:
        tariff_line = f"Текущий тариф: <b>{stats['plan_title']}</b>"

    text = (
        "👤 <b>Профиль</b>\n\n"
        f"<b>Режим по умолчанию:</b> {mode_cfg['title']}\n"
        f"<b>Сообщений всего:</b> {dossier.get('messages_count', 0)}\n"
        f"<b>Последний запрос:</b> <i>{dossier.get('last_prompt_preview', '')}</i>\n\n"
        "💳 <b>Тариф</b>\n"
        f"{tariff_line}\n"
        f"Лимит на сегодня: <b>{stats['used_today']}/{stats['limit_today']}</b>\n"
        f"Базовый лимит: <b>{stats['base_limit']}</b>\n"
        f"Бонус от рефералов: <b>{stats['ref_bonus']} (по {REF_BONUS_PER_USER} за каждого)</b>\n"
        f"Всего запросов за всё время: <b>{stats['total_requests']}</b>\n\n"
        "🎁 <b>Рефералы</b>\n"
        f"Код: <code>{stats['code'] or 'ещё не сгенерирован'}</code>\n"
        f"Приглашено: <b>{stats['invited_count']}</b> (уровень: <b>{level}</b>)\n"
    )

    await message.answer(text, reply_markup=build_main_keyboard())


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

    await message.answer(
        "Диалог очищен. Можно начинать новую ветку.",
        reply_markup=build_main_keyboard(),
    )


@router.message(Command("plans"))
async def cmd_plans(message: Message) -> None:
    """
    Обзор тарифов + оплата USDT через CryptoBot.
    """
    user_id = message.from_user.id
    limits = storage.get_limits(user_id)

    lines = ["💳 <b>Подписка</b>\n"]
    if is_admin(user_id):
        lines.append("Ты в режиме Admin — ограничений по запросам нет.\n")
    else:
        lines.append(f"Твой тариф: <b>{limits['plan_title']}</b>")
        lines.append(
            f"Лимит на сегодня: <b>{limits['used_today']}/{limits['limit_today']}</b> запросов.\n"
        )

    for key, cfg in PLAN_LIMITS.items():
        lines.append(
            f"• <b>{cfg['title']}</b> ({key}) — до <b>{cfg['daily_base']}</b> запросов в день."
        )
        desc = cfg.get("description", "").strip()
        if desc:
            lines.append(f"  {desc}\n")

    lines.append(
        f"Каждый приглашённый друг даёт +<b>{REF_BONUS_PER_USER}</b> запросов в день."
    )

    lines.append(
        "\n💰 Оплата только в USDT через @CryptoBot:\n"
        "• 7.99$ — 1 месяц\n"
        "• 26.99$ — 3 месяца\n"
        "• 82.99$ — 12 месяцев\n"
    )

    # Если заданы прямые ссылки — показываем
    if any([CRYPTO_USDT_LINK_MONTH, CRYPTO_USDT_LINK_3M, CRYPTO_USDT_LINK_YEAR]):
        lines.append("Ссылки на оплату:")
        if CRYPTO_USDT_LINK_MONTH:
            lines.append(f"• 1 месяц: {CRYPTO_USDT_LINK_MONTH}")
        if CRYPTO_USDT_LINK_3M:
            lines.append(f"• 3 месяца: {CRYPTO_USDT_LINK_3M}")
        if CRYPTO_USDT_LINK_YEAR:
            lines.append(f"• 12 месяцев: {CRYPTO_USDT_LINK_YEAR}")
    else:
        lines.append(
            "Свяжись с админом, чтобы получить актуальные ссылки на оплату и активацию тарифа."
        )

    await message.answer("\n".join(lines), reply_markup=build_main_keyboard())


# =========================
#  Кнопка «Режимы» и выбор режима
# =========================


@router.message(F.text == BTN_MODES)
async def modes_menu(message: Message) -> None:
    state = get_user_state(message.from_user.id)
    mode_cfg = ASSISTANT_MODES.get(state.mode_key, ASSISTANT_MODES[DEFAULT_MODE_KEY])

    text = (
        "Режим определяет стиль и фокус ответов.\n\n"
        f"Сейчас выбран: <b>{mode_cfg['title']}</b>.\n"
        "Выбери новый режим внизу."
    )
    await message.answer(text, reply_markup=build_modes_keyboard())


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
        await message.answer("Неизвестный режим.", reply_markup=build_main_keyboard())
        return

    state.mode_key = mode_key
    storage.update_user_mode(user_id, mode_key)

    mode_cfg = ASSISTANT_MODES[mode_key]
    limits = storage.get_limits(user_id)

    if is_admin(user_id):
        limit_line = "Лимит на сегодня: <b>без ограничений</b>."
    else:
        limit_line = f"Лимит на сегодня: <b>{limits['used_today']}/{limits['limit_today']}</b>."

    text = (
        "Режим обновлён.\n\n"
        f"Текущий режим: <b>{mode_cfg['title']}</b>\n"
        f"<i>{mode_cfg['description']}</i>\n\n"
        f"{limit_line}"
    )

    # После выбора режима возвращаем главное меню
    await message.answer(text, reply_markup=build_main_keyboard())


# =========================
#  Кнопка «Назад»
# =========================


@router.message(F.text == BTN_BACK)
async def back_to_main(message: Message) -> None:
    await message.answer("Главное меню.", reply_markup=build_main_keyboard())


# =========================
#  Сервисные разделы (Сценарии / Профиль / Реферал / Подписка)
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
        await message.answer("Раздел в разработке.", reply_markup=build_main_keyboard())
        return

    if action == "templates":
        text = (
            "⚡ <b>Сценарии</b>\n\n"
            "Примеры запросов:\n"
            "• Структура Telegram-канала\n"
            "• Идеи постов для бота\n"
            "• Разбор распорядка дня и улучшения\n\n"
            "Просто опиши свою задачу — ассистент подстроится."
        )
        kb = build_main_keyboard()

    elif action == "profile":
        user = storage.get_or_create_user(user_id)
        dossier = user.get("dossier", {})
        stats = storage.get_referral_stats(user_id)
        mode_cfg = ASSISTANT_MODES.get(
            state.mode_key,
            ASSISTANT_MODES[DEFAULT_MODE_KEY],
        )
        level = _ref_level(stats["invited_count"])

        if is_admin(user_id):
            tariff_line = "Текущий тариф: <b>Admin</b> (лимитов нет)"
        else:
            tariff_line = f"Текущий тариф: <b>{stats['plan_title']}</b>"

        text = (
            "👤 <b>Профиль</b>\n\n"
            f"<b>Режим по умолчанию:</b> {mode_cfg['title']}\n"
            f"<b>Сообщений всего:</b> {dossier.get('messages_count', 0)}\n"
            f"<b>Последний запрос:</b> <i>{dossier.get('last_prompt_preview', '')}</i>\n\n"
            "💳 <b>Тариф</b>\n"
            f"{tariff_line}\n"
            f"Лимит на сегодня: <b>{stats['used_today']}/{stats['limit_today']}</b>\n"
            f"Базовый лимит: <b>{stats['base_limit']}</b>\n"
            f"Бонус от рефералов: <b>{stats['ref_bonus']} (по {REF_BONUS_PER_USER} за каждого)</b>\n"
            f"Всего запросов за всё время: <b>{stats['total_requests']}</b>\n\n"
            "🎁 <b>Рефералы</b>\n"
            f"Код: <code>{stats['code'] or 'ещё не сгенерирован'}</code>\n"
            f"Приглашено: <b>{stats['invited_count']}</b> (уровень: <b>{level}</b>)\n"
        )
        kb = build_main_keyboard()

    elif action == "referral":
        code = storage.ensure_ref_code(user_id)
        stats = storage.get_referral_stats(user_id)
        level = _ref_level(stats["invited_count"])

        me = await message.bot.get_me()
        username = me.username or "YourBot"
        link = f"https://t.me/{username}?start=ref_{code}"

        text = (
            "🎁 <b>Реферальная программа</b>\n\n"
            f"Текущий тариф: <b>{stats['plan_title']}</b>\n"
            f"Базовый лимит: <b>{stats['base_limit']}</b>\n"
            f"Бонус от рефералов: <b>{stats['ref_bonus']} (по {REF_BONUS_PER_USER} за каждого)</b>\n"
            f"Приглашено: <b>{stats['invited_count']}</b> (уровень: <b>{level}</b>)\n\n"
            f"Твой код: <code>{code}</code>\n"
            f"Ссылка: <code>{link}</code>\n"
        )
        kb = build_main_keyboard()

    elif action == "plans":
        # просто переиспользуем /plans
        await cmd_plans(message)
        return
    else:
        text = "Раздел в разработке."
        kb = build_main_keyboard()

    await message.answer(text, reply_markup=kb)


# =========================
#  Главный LLM-handler (стриминг)
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

    # Лимиты (админ — без ограничений)
    if (not is_admin(user_id)) and (not storage.can_make_request(user_id)):
        limits = storage.get_limits(user_id)
        await message.answer(
            (
                "Лимит запросов на сегодня исчерпан.\n\n"
                f"Тариф: <b>{limits['plan_title']}</b>\n"
                f"Сегодня использовано: <b>{limits['used_today']}/{limits['limit_today']}</b>.\n\n"
                "Пригласи друзей через реферальную ссылку (кнопка «🎁 Реферал»), "
                "чтобы получить дополнительные запросы.\n\n"
                "Или открой /plans и обнови тариф."
            ),
            reply_markup=build_main_keyboard(),
        )
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    waiting_message = await message.answer(
        f"Думаю над ответом в режиме <b>{mode_cfg['title']}</b>…",
    )

    user_prompt = text.strip()
    state.last_prompt = user_prompt

    # Админа не ограничиваем, но статистику всё равно ведём
    storage.register_request(user_id)

    history = storage.get_history(user_id)

    answer_text = ""
    chunk_counter = 0
    EDIT_EVERY_N_CHUNKS = 2  # более частое редактирование для эффекта «живого» набора

    try:
        async for chunk in ask_llm_stream(state.mode_key, user_prompt, history):
            answer_text += chunk
            chunk_counter += 1

            # поддерживаем "typing..."
            if chunk_counter % 5 == 0:
                try:
                    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
                except Exception:
                    pass

            # обновляем сообщение чаще, чтобы казалось «реальным временем»
            if chunk_counter % EDIT_EVERY_N_CHUNKS == 0:
                try:
                    await waiting_message.edit_text(answer_text or "…")
                except Exception:
                    pass

        if not answer_text.strip():
            answer_text = (
                "Не удалось сформировать ответ. Попробуй переформулировать запрос."
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
            else "Произошла внутренняя ошибка. Попробуй ещё раз позже."
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
