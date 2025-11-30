from __future__ import annotations

import logging
from typing import Dict

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .ai_client import ask_ai, healthcheck_llm
from .config import settings
from .keyboards import answer_with_modes_keyboard, main_menu_keyboard, modes_keyboard
from .limits import check_rate_limit
from .memory import (
    create_conversation,
    db_healthcheck,
    get_profile,
    get_stats,
    list_conversations,
    register_user,
    save_message,
    set_profile,
)
from .modes import MODES, detect_mode
from .vision import analyze_image

logger = logging.getLogger(__name__)

router = Router(name="main")

_user_modes: Dict[int, str] = {}


def _get_user_mode(user_id: int) -> str:
    return _user_modes.get(user_id, "default")


def _set_user_mode(user_id: int, mode: str) -> None:
    if mode not in MODES:
        mode = "default"
    _user_modes[user_id] = mode


def _is_allowed(username: str | None) -> bool:
    if not settings.allowed_users:
        return True
    if not username:
        return False
    return username.lstrip("@") in settings.allowed_users


class ProfileStates(StatesGroup):
    waiting_profile = State()


class SymptomStates(StatesGroup):
    symptom = State()
    duration = State()
    details = State()
    red_flags = State()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user = message.from_user
    if not user:
        return

    if not _is_allowed(user.username):
        await message.answer(
            "🚫 Этот бот доступен ограниченному кругу пользователей.\n"
            "Если нужен доступ — напишите владельцу."
        )
        return

    register_user(user.id, user.username)
    create_conversation(user.id, "Диалог")
    _set_user_mode(user.id, "default")

    text = (
        "👋 Привет, я <b>AI Medicine Assistant</b> — твой умный мед-помощник.\n\n"
        "✨ Что я умею:\n"
        "• разбирать симптомы и объяснять, что может происходить;\n"
        "• помогать интерпретировать анализы и заключения;\n"
        "• подсказывать, к какому врачу и с чем идти;\n"
        "• помогать тебе как врачу: структурировать случаи, готовить тексты, памятки.\n\n"
        "Выбери действие ниже или просто напиши свой вопрос 👇"
    )

    await message.answer(text, reply_markup=main_menu_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "🧭 <b>Навигация</b>\n\n"
        "• /mode — выбрать профиль ответа (общий, симптомы, педиатрия и др.)\n"
        "• /symptoms — запустить пошаговый симптом-чекер\n"
        "• /profile — настроить твой профиль (пациент / врач / студент и т.п.)\n"
        "• /new — начать новый «кейс» / диалог\n"
        "• /cases — список последних кейсов\n"
        "• /stats — общая статистика (для админа)\n"
        "• /ping — healthcheck бота (для админа)\n\n"
        "Или просто напиши свой вопрос — я сам подберу подходящий режим 🤖"
    )


@router.message(Command("mode"))
async def cmd_mode(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    if not _is_allowed(user.username):
        await message.answer("🚫 Доступ запрещён.")
        return
    current = _get_user_mode(user.id)
    await message.answer(
        "Выбери режим работы ИИ 🧠:",
        reply_markup=modes_keyboard(current),
    )


@router.callback_query(F.data.startswith("set_mode:"))
async def cb_set_mode(callback: CallbackQuery) -> None:
    user = callback.from_user
    if not user:
        return

    if not _is_allowed(user.username):
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    _, mode = callback.data.split(":", 1)
    if mode not in MODES:
        await callback.answer("Неизвестный режим", show_alert=True)
        return

    _set_user_mode(user.id, mode)
    cfg = MODES[mode]
    try:
        if callback.message:
            await callback.message.edit_reply_markup(
                reply_markup=modes_keyboard(mode),
            )
    except Exception as e:
        logger.debug("edit_reply_markup failed: %s", e)

    await callback.answer(f"Режим: {cfg['short_name']}")


@router.callback_query(F.data == "menu:symptoms")
async def cb_menu_symptoms(callback: CallbackQuery, state: FSMContext) -> None:
    await cmd_symptoms_start(callback.message, state)
    await callback.answer()


@router.callback_query(F.data == "menu:profile")
async def cb_menu_profile(callback: CallbackQuery, state: FSMContext) -> None:
    await cmd_profile(callback.message, state)
    await callback.answer()


@router.callback_query(F.data == "menu:cases")
async def cb_menu_cases(callback: CallbackQuery) -> None:
    await cmd_cases(callback.message)
    await callback.answer()


@router.callback_query(F.data == "menu:ask")
async def cb_menu_ask(callback: CallbackQuery) -> None:
    await callback.message.answer("Напиши свой вопрос 👇")
    await callback.answer()


@router.message(Command("profile"))
async def cmd_profile(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if not user:
        return

    profile = get_profile(user.id)
    await state.set_state(ProfileStates.waiting_profile)

    await message.answer(
        "🧑‍⚕️ <b>Профиль пользователя</b>\n\n"
        f"Текущие настройки:\n"
        f"• Роль: <code>{profile['role']}</code>\n"
        f"• Детализация: <code>{profile['detail_level']}</code>\n"
        f"• Юрисдикция: <code>{profile['jurisdiction']}</code>\n\n"
        "Отправь одно сообщение в формате:\n"
        "<code>роль, детализация, юрисдикция</code>\n\n"
        "Примеры:\n"
        "• <i>врач, подробно, РФ</i>\n"
        "• <i>пациент, стандарт, GLOBAL</i>",
    )


@router.message(ProfileStates.waiting_profile)
async def profile_set(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if not user:
        return

    parts = [p.strip() for p in (message.text or "").split(",")]
    if len(parts) != 3:
        await message.answer(
            "Формат: <code>роль, детализация, юрисдикция</code>\n"
            "Пример: <i>врач, подробно, РФ</i>"
        )
        return

    role, detail, juris = parts
    set_profile(user.id, user.username or "", role, detail, juris)
    await state.clear()
    await message.answer("✅ Профиль обновлён.")


@router.message(Command("new"))
async def cmd_new(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    create_conversation(user.id, "Новый случай")
    await message.answer("📂 Начат новый кейс. Можешь описать ситуацию с самого начала.")


@router.message(Command("cases"))
async def cmd_cases(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    convs = list_conversations(user.id, limit=10)
    if not convs:
        await message.answer("Пока нет сохранённых кейсов. Используй /new, чтобы начать.")
        return
    lines = ["🧾 <b>Твои кейсы</b>:"]
    for c in convs:
        mark = "🟢" if c["is_active"] else "⚪️"
        ts = c["created_at"][:16]
        lines.append(f"{mark} <b>{c['title']}</b> ({ts})")
    await message.answer("\n".join(lines))


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    if not settings.admin_username or user.username is None or user.username.lstrip("@") != settings.admin_username:
        await message.answer("Команда доступна только админу.")
        return
    s = get_stats()
    await message.answer(
        f"📊 <b>Статистика</b>\n"
        f"Пользователей: <b>{s['users']}</b>\n"
        f"Сообщений: <b>{s['messages']}</b>"
    )


@router.message(Command("ping"))
async def cmd_ping(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    if not settings.admin_username or user.username is None or user.username.lstrip("@") != settings.admin_username:
        await message.answer("Команда доступна только админу.")
        return

    db_ok = db_healthcheck()
    db_line = "✅ БД отвечает" if db_ok else "❌ Проблема с БД (см. логи)"

    llm_ok, llm_msg = await healthcheck_llm()

    text = (
        "🩺 <b>Healthcheck бота</b>\n\n"
        f"🗄 База данных: {db_line}\n"
        f"🧠 Модель: {llm_msg}\n"
    )

    await message.answer(text)


@router.message(Command("symptoms"))
async def cmd_symptoms_start(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if not user:
        return
    await state.set_state(SymptomStates.symptom)
    await message.answer("🩺 Опиши главную жалобу (что беспокоит больше всего):")


@router.message(SymptomStates.symptom)
async def symptom_step_symptom(message: Message, state: FSMContext) -> None:
    await state.update_data(symptom=message.text or "")
    await state.set_state(SymptomStates.duration)
    await message.answer("⏱ Как давно это началось?")


@router.message(SymptomStates.duration)
async def symptom_step_duration(message: Message, state: FSMContext) -> None:
    await state.update_data(duration=message.text or "")
    await state.set_state(SymptomStates.details)
    await message.answer("➕ Есть ли ещё симптомы? Температура, слабость, сыпь и т.п.")


@router.message(SymptomStates.details)
async def symptom_step_details(message: Message, state: FSMContext) -> None:
    await state.update_data(details=message.text or "")
    await state.set_state(SymptomStates.red_flags)
    await message.answer(
        "🚨 Есть ли «красные флаги»:\n"
        "• сильная боль\n"
        "• затруднённое дыхание\n"
        "• потеря сознания\n"
        "• резкое ухудшение состояния\n\n"
        "Если ничего из этого нет — напиши «нет»."
    )


@router.message(SymptomStates.red_flags)
async def symptom_step_red_flags(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if not user:
        return

    await state.update_data(red_flags=message.text or "")
    data = await state.get_data()
    await state.clear()

    text = (
        "Симптом-чекер 👇\n\n"
        f"Главная жалоба: {data.get('symptom')}\n"
        f"Длительность: {data.get('duration')}\n"
        f"Доп. симптомы: {data.get('details')}\n"
        f"Красные флаги: {data.get('red_flags')}\n\n"
        "На основе этих данных:\n"
        "• определи вероятные причины;\n"
        "• опиши, что настораживает;\n"
        "• перечисли красные флаги и когда нужно срочно обращаться;\n"
        "• предложи план действий и вопросы к врачу."
    )

    ok, _, msg = check_rate_limit(user.id)
    if not ok:
        await message.answer(msg or "⏳ Лимит запросов, попробуй позже.")
        return

    mode = "symptoms"
    _set_user_mode(user.id, mode)
    reply = await ask_ai(user.id, mode, text)
    await message.answer(
        reply,
        reply_markup=answer_with_modes_keyboard(mode),
    )


@router.message(F.photo)
async def photo_handler(message: Message, bot: Bot) -> None:
    user = message.from_user
    if not user:
        return

    if not _is_allowed(user.username):
        await message.answer("🚫 Доступ запрещён.")
        return

    ok, _, msg = check_rate_limit(user.id)
    if not ok:
        await message.answer(msg or "⏳ Лимит запросов, попробуй позже.")
        return

    photo = message.photo[-1]
    file = await bot.download(photo)
    image_bytes = file.read()

    await message.answer("📷 Получил изображение, анализирую...")
    reply = await analyze_image(image_bytes, user_id=user.id)
    save_message(user.id, "user", "[изображение]", "vision")
    save_message(user.id, "assistant", reply, "vision")

    await message.answer(
        reply,
        reply_markup=answer_with_modes_keyboard(_get_user_mode(user.id)),
    )


@router.message(F.text)
async def text_handler(message: Message) -> None:
    user = message.from_user
    if not user:
        return

    if not _is_allowed(user.username):
        await message.answer("🚫 Доступ запрещён.")
        return

    ok, _, msg = check_rate_limit(user.id)
    if not ok:
        await message.answer(msg or "⏳ Лимит запросов, попробуй позже.")
        return

    text = message.text or ""
    current_mode = _get_user_mode(user.id)
    detected_mode = detect_mode(text, current_mode=current_mode)
    if detected_mode != current_mode:
        _set_user_mode(user.id, detected_mode)
        mode_note = f"🔁 Переключился в режим: <b>{MODES[detected_mode]['short_name']}</b>\n\n"
    else:
        mode_note = ""

    await message.chat.do("typing")

    reply = await ask_ai(user.id, detected_mode, text)
    final = mode_note + reply

    await message.answer(
        final,
        reply_markup=answer_with_modes_keyboard(detected_mode),
    )


@router.callback_query(F.data.startswith("act:"))
async def cb_answer_action(callback: CallbackQuery) -> None:
    user = callback.from_user
    if not user or not callback.message:
        await callback.answer()
        return

    ok, _, msg = check_rate_limit(user.id)
    if not ok:
        await callback.answer(msg or "⏳ Лимит запросов, попробуй позже.", show_alert=True)
        return

    original = callback.message.text or ""
    action = callback.data.split(":", 1)[1]

    if action == "summary":
        prompt = (
            "Сделай краткий конспект (3–5 пунктов) из следующего текста, чтобы быстро вспомнить суть:\n\n"
            + original
        )
    elif action == "followup":
        prompt = (
            "Предложи список из 5–7 уточняющих вопросов, которые стоит задать врачу по следующему ответу:\n\n"
            + original
        )
    elif action == "for_patient":
        prompt = (
            "Перепиши следующий текст простым языком для пациента, сохранив смысл и важные предостережения:\n\n"
            + original
        )
    else:
        await callback.answer()
        return

    reply = await ask_ai(user.id, _get_user_mode(user.id), prompt)
    await callback.message.reply(reply)
    await callback.answer("Готово ✅")
