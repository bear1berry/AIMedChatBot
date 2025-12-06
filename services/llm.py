from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

# Импортируем config единым модулем, чтобы не ловить ImportError из-за отсутствующих констант
import bot.config as config

logger = logging.getLogger(__name__)

DEEPSEEK_API_KEY: str = getattr(config, "DEEPSEEK_API_KEY", "")
DEEPSEEK_API_URL: str = getattr(config, "DEEPSEEK_API_URL", "")
DEFAULT_MODEL: str = getattr(config, "DEEPSEEK_MODEL", "deepseek-chat")

DEEPSEEK_LIGHT_MODEL: str = getattr(config, "DEEPSEEK_LIGHT_MODEL", DEFAULT_MODEL)
DEEPSEEK_HEAVY_MODEL: str = getattr(config, "DEEPSEEK_HEAVY_MODEL", DEFAULT_MODEL)

ASSISTANT_MODES: Dict[str, Dict[str, Any]] = getattr(config, "ASSISTANT_MODES", {})
DEFAULT_MODE_KEY: str = getattr(config, "DEFAULT_MODE_KEY", "universal")


@dataclass
class Intent:
    """
    Простая модель интента.
    kind:
      - "question"
      - "plan"
      - "brainstorm"
      - "reflection"
      - "emotional"
      - "other"
    """
    kind: str
    is_long: bool
    raw_text: str


def _estimate_tokens(text: str) -> int:
    # Грубая оценка: ~4 символа на токен
    return max(1, len(text) // 4)


def analyze_intent(message_text: str) -> Intent:
    """
    Лёгкий анализ интента для дальнейшей маршрутизации.
    На первых порах — чистые эвристики без LLM.
    """
    text = (message_text or "").strip().lower()
    is_long = len(text) > 300

    # очень грубые эвристики
    if any(w in text for w in ["план", "структурируй", "шаги", "чек-лист", "чеклист"]):
        kind = "plan"
    elif any(w in text for w in ["вариант", "варианты", "брейншторм", "идея", "идеи"]):
        kind = "brainstorm"
    elif any(w in text for w in ["чувствую", "переживаю", "тревога", "стресс", "перегруз", "не знаю что делать"]):
        kind = "emotional"
    elif any(w in text for w in ["почему", "зачем", "как", "что такое", "что делать", "?"]):
        kind = "question"
    elif any(w in text for w in ["рефлексия", "подведи итоги", "подытожим", "итоги дня"]):
        kind = "reflection"
    else:
        kind = "other"

    return Intent(kind=kind, is_long=is_long, raw_text=message_text)


def _detect_emotion(message_text: str) -> str:
    """
    Очень лёгкий «эмоциональный радар».
    Возвращает один из тегов:
    - overload / anxiety / anger / inspired / apathy / neutral
    """
    text = (message_text or "").strip().lower()

    overload_words = ["перегруз", "слишком много", "не успеваю", "устал", "голова кипит"]
    anxiety_words = ["тревога", "переживаю", "волнует", "страх", "нервничаю"]
    anger_words = ["злюсь", "бесит", "раздражает", "ненавижу"]
    inspired_words = ["заряжен", "вдохновлен", "вдохновлён", "кайф", "огонь"]
    apathy_words = ["апатия", "пусто", "ничего не хочется", "нет сил"]

    def _has(words: List[str]) -> bool:
        return any(w in text for w in words)

    if _has(overload_words):
        return "overload"
    if _has(anxiety_words):
        return "anxiety"
    if _has(anger_words):
        return "anger"
    if _has(inspired_words):
        return "inspired"
    if _has(apathy_words):
        return "apathy"
    return "neutral"


def _select_model_for_prompt(intent: Intent, mode_key: str) -> str:
    """
    Умный выбор модели:
    - лёгкие / small-talk → DEEPSEEK_LIGHT_MODEL
    - длинные, сложные, бизнес/медицина/наставник → DEEPSEEK_HEAVY_MODEL
    """
    mode_key = (mode_key or DEFAULT_MODE_KEY).lower()

    # тяжелые режимы по умолчанию
    heavy_modes = {"business", "medicine", "coach"}

    if mode_key in heavy_modes:
        return DEEPSEEK_HEAVY_MODEL

    # длинные и «план/рефлексия/эмоции» тоже на heavy
    if intent.is_long or intent.kind in {"plan", "reflection", "emotional"}:
        return DEEPSEEK_HEAVY_MODEL

    # всё остальное можно на лёгкую
    return DEEPSEEK_LIGHT_MODEL


def _build_system_prompt(
    mode_key: str,
    style_hint: str,
    emotion_tag: str,
    is_premium: bool = False,
) -> str:
    """
    Собираем системный промпт на основе:
    - выбранного режима (медицина, бизнес, наставник и т.д.)
    - стилистики (ты/вы, формальность, плотность структуры)
    - эмоционального состояния (чуть больше мягкости/структуры и т.п.)
    - премиум-режима «стратегический мозг»
    """
    mode_key = mode_key or DEFAULT_MODE_KEY
    mode = ASSISTANT_MODES.get(mode_key, ASSISTANT_MODES.get(DEFAULT_MODE_KEY, {}))

    base_prompt = mode.get("system_prompt", "").strip()
    behavior_rules = mode.get("behavior_rules", "").strip()

    # лёгкая настройка под эмоцию — без прямого «я вижу, ты тревожишься»
    emotion_suffix = ""
    if emotion_tag == "overload":
        emotion_suffix = (
            "\n\nДополнительно: пользователь сейчас перегружен. "
            "Не усложняй, упрощай и структурируй. Делай ответы по шагам, без лишнего шума."
        )
    elif emotion_tag == "anxiety":
        emotion_suffix = (
            "\n\nДополнительно: пользователь испытывает тревогу. "
            "Пиши спокойно, ровно, без катастрофизации. Помогай структурировать ситуацию."
        )
    elif emotion_tag == "anger":
        emotion_suffix = (
            "\n\nДополнительно: пользователь раздражён. "
            "Будь прямым, но без конфронтации. Уводи в конструктив и конкретику."
        )
    elif emotion_tag == "inspired":
        emotion_suffix = (
            "\n\nДополнительно: пользователь заряжен и мотивирован. "
            "Можно давать чуть более смелые идеи и вызовы, но без лишнего пафоса."
        )
    elif emotion_tag == "apathy":
        emotion_suffix = (
            "\n\nДополнительно: у пользователя апатия/усталость. "
            "Делай ответы короткими, максимально прикладными, с микрошагами."
        )

    style_suffix = ""
    if style_hint:
        style_suffix = (
            "\n\nСтиль общения:\n"
            f"{style_hint.strip()}"
        )

    premium_suffix = ""
    if is_premium:
        premium_suffix = (
            "\n\nПремиум-режим «стратегический мозг»:\n"
            "- давай более глубокие ответы с чёткой структурой (заголовки, списки, блоки);\n"
            "- предлагай несколько вариантов, гипотез и сценариев, а не один очевидный путь;\n"
            "- иллюстрируй ключевые идеи короткими, но ёмкими примерами из жизни/бизнеса;\n"
            "- не растекайся: максимум смысла на единицу текста, минимум воды."
        )

    parts = [base_prompt, behavior_rules, style_suffix, emotion_suffix, premium_suffix]
    final = "\n\n".join(p for p in parts if p)
    if not final:
        final = (
            "Ты — умный, внимательный и честный ассистент. "
            "Отвечай структурировано, на чистом русском языке, без лишней воды."
        )
    return final


async def _call_deepseek(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 1024,
) -> Dict[str, Any]:
    """
    Один вызов DeepSeek Chat Completion.
    Возвращает dict с ключами:
      - content: текст ответа
      - total_tokens: оценка/usage
    """
    if not DEEPSEEK_API_KEY or not DEEPSEEK_API_URL:
        raise RuntimeError("DeepSeek API не настроен: DEEPSEEK_API_KEY/DEEPSEEK_API_URL пустые.")

    model_name = model or DEFAULT_MODEL

    payload: Dict[str, Any] = {
        "model": model_name,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(DEEPSEEK_API_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    try:
        content = data["choices"][0]["message"]["content"]
    except Exception:
        logger.error("Unexpected DeepSeek response structure: %r", data)
        raise

    usage = data.get("usage", {}) or {}
    total_tokens = usage.get("total_tokens") or usage.get("completion_tokens")
    if total_tokens is None:
        total_tokens = _estimate_tokens(content)

    return {
        "content": content,
        "total_tokens": int(total_tokens),
    }


def _split_into_chunks(text: str, target_size: int = 400) -> List[str]:
    """
    Делит текст на смысловые чанки:
    - сначала по двойным переносам (абзацы),
    - если абзац слишком длинный — режем его дополнительно.
    """
    text = (text or "").strip()
    if not text:
        return []

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: List[str] = []

    for para in paragraphs:
        if len(para) <= target_size:
            chunks.append(para)
        else:
            # режем длинный абзац на куски по target_size
            start = 0
            while start < len(para):
                end = start + target_size
                chunks.append(para[start:end])
                start = end

    # добавим двойной перенос между чанками, чтобы сохранялась структура
    merged: List[str] = []
    for i, ch in enumerate(chunks):
        if i == 0:
            merged.append(ch)
        else:
            merged.append("\n\n" + ch)
    return merged


async def ask_llm_stream(
    mode_key: str,
    user_prompt: str,
    style_hint: str = "",
    is_premium: bool = False,
) -> AsyncIterator[Dict[str, Any]]:
    """
    Главный метод для ядра:
    - анализирует интент и эмоцию,
    - выбирает модель,
    - собирает системный промпт (для премиум — «стратегический мозг»),
    - делает один запрос к DeepSeek,
    - режет ответ на чанки и стримит их наружу.
    Каждая итерация возвращает dict:
      {
        "delta": <последний чанк>,
        "full": <полный текст на данный момент>,
        "tokens": <кол-во токенов, только на последнем чанке ненулевое>
      }
    """
    intent = analyze_intent(user_prompt)
    emotion_tag = _detect_emotion(user_prompt)
    model_name = _select_model_for_prompt(intent, mode_key)

    system_prompt = _build_system_prompt(
        mode_key=mode_key,
        style_hint=style_hint,
        emotion_tag=emotion_tag,
        is_premium=is_premium,
    )

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    # Премиум получает больший лимит токенов на ответ
    max_tokens = 2048 if is_premium else 1024

    result = await _call_deepseek(messages, model=model_name, max_tokens=max_tokens)
    full_text = result["content"]
    total_tokens = result["total_tokens"]

    chunks = _split_into_chunks(full_text)
    if not chunks:
        # даже если LLM вернул пустоту, возвращаем один пустой чанк
        yield {"delta": "", "full": "", "tokens": total_tokens}
        return

    assembled = ""
    for i, ch in enumerate(chunks):
        assembled += ch
        # только на последнем чанке передаём количество токенов
        tokens = total_tokens if i == len(chunks) - 1 else 0
        yield {
            "delta": ch,
            "full": assembled,
            "tokens": tokens,
        }


async def make_daily_summary(messages_texts: List[str]) -> str:
    """
    Делает короткий дневной summary (3–5 тезисов + общий вектор) по текстам пользователя за день.
    Используем тяжёлую модель, чтобы качество было максимально высоким.
    """
    joined = "\n\n".join(m.strip() for m in messages_texts if m.strip())
    if not joined:
        return ""

    system_prompt = (
        "Ты выступаешь как персональный аналитик и наставник. "
        "По фрагментам переписки за день сделай краткое дневное резюме для пользователя.\n"
        "- Сформулируй 3–5 ключевых тезисов (что он делал, о чём думал, какие решения/выводы звучали).\n"
        "- Отметь общий вектор дня: прогресс, застой, расфокус, перегруз и т.п.\n"
        "- Пиши по-деловому, лаконично, без воды, без сюсюканья.\n"
        "- Обращайся на 'ты'."
    )

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": joined},
    ]

    result = await _call_deepseek(messages, model=DEEPSEEK_HEAVY_MODEL)
    summary = result["content"] or ""
    return summary.strip()
