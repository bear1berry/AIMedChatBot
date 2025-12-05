from __future__ import annotations

from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .subscription_db import (
    list_projects,
    create_project,
    get_current_project,
    set_current_project,
    get_project,
    archive_project,
)

router = Router(name="projects")


# === FSM для создания проекта ===

class NewProject(StatesGroup):
    waiting_for_title = State()
    waiting_for_description = State()
    waiting_for_prompt = State()


# === Хелперы клавиатур ===

def _projects_keyboard(
    telegram_id: int,
    current_project_id: int | None,
    projects: list[dict],
) -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()

    if not projects:
        kb.button(text="➕ Создать первый проект", callback_data="pr:new")
    else:
        for p in projects:
            text = p["title"]
            if current_project_id == p["id"]:
                text = f"✅ {text}"
            kb.button(
                text=text,
                callback_data=f"pr:set:{p['id']}",
            )
        kb.button(text="➕ Новый проект", callback_data="pr:new")

    kb.adjust(1)
    return kb.as_markup()


async def _send_projects_menu(
    message: types.Message | types.CallbackQuery,
    state: FSMContext | None = None,
):
    """
    Показать список проектов пользователю.
    """
    if isinstance(message, types.CallbackQuery):
        user = message.from_user
        chat = message.message
    else:
        user = message.from_user
        chat = message

    telegram_id = user.id
    projects = list_projects(telegram_id)
    current = get_current_project(telegram_id)
    current_id = current["id"] if current else None

    text_lines = [
        "📂 *Твои проекты*",
        "",
        "Проект — это отдельный контекст/режим работы бота.",
        "Можно завести, например:",
        "• «Личный рост и прокачка»",
        "• «Телеграм-канал AI Medicine»",
        "• «Бизнес-идеи и стартапы»",
        "",
        "Выбери активный проект или создай новый.",
    ]
    text = "\n".join(text_lines)

    if isinstance(message, types.CallbackQuery):
        await chat.edit_text(
            text,
            reply_markup=_projects_keyboard(telegram_id, current_id, projects),
            parse_mode="Markdown",
        )
        await message.answer()  # убираем "часики"
    else:
        await chat.answer(
            text,
            reply_markup=_projects_keyboard(telegram_id, current_id, projects),
            parse_mode="Markdown",
        )


# === Команды /projects ===

@router.message(Command("projects"))
async def cmd_projects(message: types.Message, state: FSMContext):
    await _send_projects_menu(message, state)


# === Callback-обработчики ===

@router.callback_query(F.data == "pr:new")
async def cb_new_project(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(NewProject.waiting_for_title)
    await call.message.edit_text(
        "🆕 Создаём новый проект.\n\n"
        "Напиши название проекта (кратко, по делу):",
    )
    await call.answer()


@router.message(NewProject.waiting_for_title)
async def project_title_step(message: types.Message, state: FSMContext):
    title = (message.text or "").strip()
    if not title:
        await message.answer("Название не может быть пустым. Напиши ещё раз:")
        return

    await state.update_data(title=title)
    await state.set_state(NewProject.waiting_for_description)
    await message.answer(
        f"Отлично, проект называется *{title}*.\n\n"
        "Теперь опиши кратко, для чего он нужен. "
        "Это поможет боту лучше понимать контекст.",
        parse_mode="Markdown",
    )


@router.message(NewProject.waiting_for_description)
async def project_description_step(message: types.Message, state: FSMContext):
    description = (message.text or "").strip()
    await state.update_data(description=description)
    await state.set_state(NewProject.waiting_for_prompt)
    await message.answer(
        "И напоследок — задай системный промпт/фокус для этого проекта.\n\n"
        "Например:\n"
        "«Ты — эксперт по медицинским стартапам, помогаешь находить ниши…»",
    )


@router.message(NewProject.waiting_for_prompt)
async def project_prompt_step(message: types.Message, state: FSMContext):
    data = await state.get_data()
    title = data.get("title", "Проект")
    description = data.get("description", "")
    system_prompt = (message.text or "").strip()

    project = create_project(
        telegram_id=message.from_user.id,
        title=title,
        description=description,
        system_prompt=system_prompt,
    )

    # делаем новый проект активным
    set_current_project(message.from_user.id, project.get("id"))
    await state.clear()

    await message.answer(
        "✅ Проект создан и выбран активным.\n\n"
        f"*{title}*\n"
        f"_{description}_",
        parse_mode="Markdown",
    )

    # показываем обновлённое меню проектов
    await _send_projects_menu(message, None)


@router.callback_query(F.data.startswith("pr:set:"))
async def cb_set_project(call: types.CallbackQuery, state: FSMContext):
    try:
        _, _, raw_id = call.data.split(":", 2)
        project_id = int(raw_id)
    except Exception:
        await call.answer("Ошибка идентификатора проекта.", show_alert=True)
        return

    project = get_project(call.from_user.id, project_id)
    if not project:
        await call.answer("Проект не найден.", show_alert=True)
        return

    set_current_project(call.from_user.id, project_id)
    await call.answer(f"Активный проект: {project['title']}", show_alert=False)
    await _send_projects_menu(call, state)


@router.callback_query(F.data.startswith("pr:archive:"))
async def cb_archive_project(call: types.CallbackQuery, state: FSMContext):
    try:
        _, _, raw_id = call.data.split(":", 2)
        project_id = int(raw_id)
    except Exception:
        await call.answer("Ошибка идентификатора проекта.", show_alert=True)
        return

    project = get_project(call.from_user.id, project_id)
    if not project:
        await call.answer("Проект не найден.", show_alert=True)
        return

    archive_project(call.from_user.id, project_id)
    await call.answer("Проект отправлен в архив.")
    await _send_projects_menu(call, state)
