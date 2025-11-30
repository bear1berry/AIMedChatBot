from aiogram import Router, F
from aiogram.types import Message
from aiogram.filters import CommandStart

from .ai_client import ask_ai

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я AI Medicine Bot 💊\n\n"
        "Отвечаю на общие медицинские вопросы в формате просвещения: "
        "объясняю симптомы, анализы, подходы к лечению простым языком.\n\n"
        "Я не ставлю диагнозы и не заменяю очную консультацию врача."
    )


@router.message(F.text)
async def handle_text(message: Message):
    # Сообщение-заглушка, пока модель думает
    waiting = await message.answer("Думаю над ответом… 1–2 секунды 🧠")

    reply = await ask_ai(message.text)

    await waiting.edit_text(reply)
