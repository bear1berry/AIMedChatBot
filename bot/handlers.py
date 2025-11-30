from aiogram import Router, F
from aiogram.types import Message
from aiogram.filters import CommandStart, Command

from .config import settings
from .ai_client import ask_ai
from .vision import analyze_image
from .modes import MODES
from .memory import register_user, active_users

router = Router()

# Храним текущие режимы пользователей
user_modes = {}


def check_access(username: str) -> bool:
    return username.lower() in [u.lower() for u in settings.allowed_users]


def is_admin(username: str) -> bool:
    return username.lower() == settings.admin_user.lower()


# =======================
#        /start
# =======================

@router.message(CommandStart())
async def start(message: Message):
    username = message.from_user.username or ""

    if not check_access(username):
        await message.answer("🚫 У вас нет доступа к этому боту.")
        return

    # Регистрируем пользователя
    register_user(message.from_user.id, username)

    # Устанавливаем дефолтный режим
    user_modes[message.from_user.id] = "default"

    await message.answer(
        "Привет! Я AI Medicine Bot.\n\n"
        "Доступные режимы:\n"
        "• /mode_default — обычный\n"
        "• /mode_simple — простым языком\n"
        "• /mode_medical — справочник\n"
        "• /mode_symptoms — анализ симптомов\n\n"
        "Отправь вопрос 👇"
    )


# =======================
#       /users  (admin)
# =======================

@router.message(Command("users"))
async def cmd_users(message: Message):
    username = message.from_user.username or ""

    if not is_admin(username):
        return await message.answer("🚫 Только администратор может выполнять эту команду.")

    if not active_users:
        return await message.answer("Пока никто не обращался к боту.")

    text = "📋 Список подключённых пользователей:\n\n"
    for uid, uname in active_users.items():
        text += f"• @{uname} (ID: {uid})\n"

    await message.answer(text)


# =======================
#       Режимы
# =======================

@router.message(Command("mode_default"))
async def m_default(msg: Message):
    user_modes[msg.from_user.id] = "default"
    await msg.answer("Режим: Обычный 💬")


@router.message(Command("mode_simple"))
async def m_simple(msg: Message):
    user_modes[msg.from_user.id] = "simple"
    await msg.answer("Режим: Простыми словами 🧠")


@router.message(Command("mode_medical"))
async def m_medical(msg: Message):
    user_modes[msg.from_user.id] = "medical"
    await msg.answer("Режим: Медицинский справочник 📚")


@router.message(Command("mode_symptoms"))
async def m_symptoms(msg: Message):
    user_modes[msg.from_user.id] = "symptoms"
    await msg.answer("Режим: Анализ симптомов 🔍")


# =======================
#     Анализ фото
# =======================

@router.message(F.photo)
async def photo_handler(message: Message):
    username = message.from_user.username or ""

    if not check_access(username):
        return await message.answer("🚫 Нет доступа.")

    register_user(message.from_user.id, username)

    file_id = message.photo[-1].file_id
    file = await message.bot.get_file(file_id)
    file_bytes = await message.bot.download_file(file.file_path)

    await message.answer("🔍 Анализирую изображение…")

    result = await analyze_image(file_bytes.read())
    await message.answer(result)


# =======================
#        Текст
# =======================

@router.message(F.text)
async def text_handler(message: Message):
    username = message.from_user.username or ""

    if not check_access(username):
        return await message.answer("🚫 Нет доступа.")

    register_user(message.from_user.id, username)

    mode = user_modes.get(message.from_user.id, "default")
    await message.answer("Думаю над ответом… 🧠")

    reply = await ask_ai(
        user_id=message.from_user.id,
        mode=mode,
        user_message=message.text
    )

    await message.answer(reply)
