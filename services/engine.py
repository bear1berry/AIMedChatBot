from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import time
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# DB и LLM-конфиг (без привязки к Telegram)
DB_PATH = os.getenv("DB_PATH", "aimedbot.db")

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-70b-versatile")

LLM_AVAILABLE = bool(DEEPSEEK_API_KEY or GROQ_API_KEY)


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Режимы (общие для движка и UI)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModeConfig:
    key: str
    title: str
    button_text: str
    short_label: str
    description: str
    system_suffix: str


DEFAULT_MODE_KEY = "universal"

MODE_CONFIGS: Dict[str, ModeConfig] = {
    "universal": ModeConfig(
        key="universal",
        title="Универсальный режим",
        button_text="🧠 Универсальный",
        short_label="универсальный умный собеседник",
        description=(
            "Режим по умолчанию: подходит и для размышлений, и для задач, и для текстов. "
            "Баланс между глубиной и скоростью ответа."
        ),
        system_suffix=(
            "Ты работаешь в универсальном режиме. "
            "Главная цель — быстро и по-человечески помочь пользователю разобраться в вопросе. "
            "Избегай штампов и размытых формулировок, отвечай структурно."
        ),
    ),
    "medical": ModeConfig(
        key="medical",
        title="Медицинский режим",
        button_text="🩺 Медицина",
        short_label="аккуратный медицинский помощник",
        description=(
            "Осторожные, проверенные ответы по здоровью. "
            "Всегда с дисклеймером, что это не замена очной консультации."
        ),
        system_suffix=(
            "Сейчас ты работаешь в медицинском режиме. "
            "Давай только общеобразовательную информацию, опираясь на доказательный подход. "
            "Никогда не ставь диагнозы и не давай прямых назначений лекарств.\n\n"
            "Структура ответа:\n"
            "1) Кратко переформулируй запрос.\n"
            "2) Возможные объяснения и факторы — без категоричности.\n"
            "3) Что можно сделать аккуратно и безопасно до визита к врачу.\n"
            "4) Когда нужно немедленно обратиться за очной помощью.\n"
            "5) В конце добавь блок «⚠️ Важно», что это не замена консультации врача."
        ),
    ),
    "mentor": ModeConfig(
        key="mentor",
        title="Наставник",
        button_text="🔥 Наставник",
        short_label="личный наставник и коуч",
        description=(
            "Фокус на личном росте, дисциплине и мышлении. "
            "В каждом ответе есть конкретные шаги и вопросы для самоанализа."
        ),
        system_suffix=(
            "Сейчас ты работаешь в режиме наставника и коуча. "
            "Твоя задача — усиливать стержень пользователя и помогать ему двигаться вперёд.\n\n"
            "Каждый ответ обязательно завершай блоком «👉 Конкретные шаги на сегодня» из 1–3 пунктов. "
            "В большинстве ответов задавай в конце один точный вопрос для саморефлексии."
        ),
    ),
    "business": ModeConfig(
        key="business",
        title="Бизнес-архитектор",
        button_text="💼 Бизнес",
        short_label="бизнес-архитектор",
        description=(
            "Режим для стратегий, запусков и денег. "
            "Максимум конкретики: цифры, гипотезы, тесты, сценарии."
        ),
        system_suffix=(
            "Сейчас ты работаешь в режиме бизнес-архитектора. "
            "Фокус — деньги, эффективность и проверяемые гипотезы.\n\n"
            "Используй язык цифр и метрик там, где это уместно. "
            "В ответах добавляй два блока:\n"
            "• «📊 Что проверить» — ключевые допущения и риски.\n"
            "• «🧪 Как протестировать» — простые шаги для MVP и smoke-тестов."
        ),
    ),
    "creative": ModeConfig(
        key="creative",
        title="Креативный режим",
        button_text="🎨 Креатив",
        short_label="креативный генератор идей",
        description=(
            "Подходит для идей, образов, текстов и необычных решений. "
            "Более свободный стиль, но без потери структуры."
        ),
        system_suffix=(
            "Сейчас ты работаешь в креативном режиме. "
            "Твоя задача — выдавать богатый спектр идей и неожиданных решений, "
            "не забывая про практическую применимость.\n\n"
            "Предлагай несколько подходов, давай варианты формулировок, названий, визуальных концептов."
        ),
    ),
}

BASE_SYSTEM_PROMPT = (
    "Ты — BlackBox GPT, универсальный ИИ-ассистент в Telegram.\n"
    "Интерфейс — минималистичный чат: никакого визуального шума, только текст высокого качества.\n"
    "Твои ответы должны восприниматься как работа премиум-уровня: ясная структура, аккуратный язык, уважительный тон.\n"
    "Всегда отвечай на русском языке, если явно не попросили другой язык.\n"
)


# ---------------------------------------------------------------------------
# Style Engine 2.0
# ---------------------------------------------------------------------------

@dataclass
class StyleProfile:
    address: str = "ty"  # 'ty' / 'vy'
    formality: float = 0.5
    structure_density: float = 0.5
    explanation_depth: float = 0.5
    fire_level: float = 0.3
    updated_at_ts: float = field(default_factory=lambda: time.time())


def save_message(telegram_id: int, role: str, content: str) -> None:
    content = (content or "").strip()
    if not content:
        return

    ts = int(time.time())
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO messages (telegram_id, role, content, created_at_ts)
        VALUES (?, ?, ?, ?)
        """,
        (telegram_id, role, content, ts),
    )
    conn.commit()
    conn.close()


def get_recent_user_messages(telegram_id: int, limit: int = 30) -> List[str]:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT content
        FROM messages
        WHERE telegram_id = ? AND role = 'user'
        ORDER BY created_at_ts DESC
        LIMIT ?
        """,
        (telegram_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return [row["content"] for row in reversed(rows)]


def get_recent_dialog_history(telegram_id: int, limit: int = 12) -> List[Dict[str, str]]:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT role, content
        FROM messages
        WHERE telegram_id = ?
        ORDER BY created_at_ts DESC
        LIMIT ?
        """,
        (telegram_id, limit),
    )
    rows = cur.fetchall()
    conn.close()

    history: List[Dict[str, str]] = []
    for row in reversed(rows):
        role = "assistant" if row["role"] == "assistant" else "user"
        history.append({"role": role, "content": row["content"]})
    return history


def _style_profile_from_dict(data: Dict[str, Any]) -> StyleProfile:
    return StyleProfile(
        address=str(data.get("address", "ty")) if data.get("address") in {"ty", "vy"} else "ty",
        formality=float(data.get("formality", 0.5)),
        structure_density=float(data.get("structure_density", 0.5)),
        explanation_depth=float(data.get("explanation_depth", 0.5)),
        fire_level=float(data.get("fire_level", 0.3)),
        updated_at_ts=float(data.get("updated_at_ts", time.time())),
    )


def _load_style_profile(telegram_id: int) -> Optional[StyleProfile]:
    conn = _get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT style_profile_json FROM users_v2 WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = cur.fetchone()
    except sqlite3.OperationalError:
        conn.close()
        return None
    conn.close()

    if not row:
        return None

    raw = row["style_profile_json"]
    if not raw:
        return None

    try:
        data = json.loads(raw)
        return _style_profile_from_dict(data)
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to parse style_profile_json for %s: %r", telegram_id, e)
        return None


def _save_style_profile(telegram_id: int, profile: StyleProfile) -> None:
    conn = _get_conn()
    cur = conn.cursor()
    data_json = json.dumps(asdict(profile), ensure_ascii=False)
    cur.execute(
        """
        UPDATE users_v2
        SET style_profile_json = ?, updated_at_ts = ?
        WHERE telegram_id = ?
        """,
        (data_json, int(time.time()), telegram_id),
    )
    conn.commit()
    conn.close()


def _instant_style_from_messages(messages: List[str]) -> StyleProfile:
    if not messages:
        return StyleProfile()

    joined = " ".join(messages)
    lower = joined.lower()

    # обращение / формальность
    formal_markers = ["здравствуйте", "добрый день", "добрый вечер", "уважаем", "будьте добры"]
    slang_markers = ["чувак", "бро", "фигня", "жесть", "капец"]

    uses_vy = any(m in lower for m in formal_markers) or " вы " in lower
    uses_ty_slang = any(m in lower for m in slang_markers) or " ты " in lower

    if uses_vy and not uses_ty_slang:
        address = "vy"
        formality = 0.85
    elif uses_ty_slang and not uses_vy:
        address = "ty"
        formality = 0.25
    else:
        address = "ty"
        formality = 0.5

    has_lists = any(
        marker in joined
        for marker in ["\n- ", "\n•", "\n1.", "\n1)", "1) ", "1. "]
    )
    structure_density = 0.75 if has_lists else 0.35

    lengths = [len(m) for m in messages if m.strip()]
    avg_len = sum(lengths) / len(lengths) if lengths else 0
    if avg_len < 80:
        explanation_depth = 0.25
    elif avg_len < 220:
        explanation_depth = 0.5
    else:
        explanation_depth = 0.8

    fire_level = 0.3
    strong_words = [
        "нах",
        "хрен",
        "черт",
        "чёрт",
        "дерьмо",
        "сраная",
        "сраный",
        "жестко",
        "жёстко",
        "рубить правду",
        "по-жёсткому",
    ]
    soft_words = ["помягче", "бережно", "аккуратнее"]

    if any(w in lower for w in strong_words):
        fire_level = 0.7
    if any(w in lower for w in soft_words):
        fire_level = 0.2

    return StyleProfile(
        address=address,
        formality=formality,
        structure_density=structure_density,
        explanation_depth=explanation_depth,
        fire_level=fire_level,
    )


def build_style_profile_from_history(telegram_id: int) -> StyleProfile:
    messages = get_recent_user_messages(telegram_id, limit=30)
    snapshot = _instant_style_from_messages(messages)
    prev = _load_style_profile(telegram_id)

    if not prev:
        profile = snapshot
    else:
        alpha = 0.25
        profile = StyleProfile(
            address=snapshot.address if snapshot.address != prev.address else prev.address,
            formality=prev.formality * (1 - alpha) + snapshot.formality * alpha,
            structure_density=prev.structure_density * (1 - alpha)
            + snapshot.structure_density * alpha,
            explanation_depth=prev.explanation_depth * (1 - alpha)
            + snapshot.explanation_depth * alpha,
            fire_level=prev.fire_level * (1 - alpha) + snapshot.fire_level * alpha,
            updated_at_ts=time.time(),
        )

    _save_style_profile(telegram_id, profile)
    return profile


def style_profile_to_hint(profile: StyleProfile) -> str:
    parts: List[str] = ["Адаптируй стиль под пользователя."]

    if profile.address == "vy":
        parts.append("Обращайся к пользователю на «Вы», без фамильярности.")
    else:
        parts.append("Обращайся к пользователю на «ты», живо, но без панибратства.")

    if profile.formality > 0.7:
        parts.append("Стиль ближе к деловому: аккуратные формулировки, минимум сленга.")
    elif profile.formality < 0.3:
        parts.append("Стиль ближе к разговорному: допускается живой язык, но без грубостей.")
    else:
        parts.append("Стиль нейтральный: можно чуть живого языка, но без канцелярита и без жаргона.")

    if profile.structure_density > 0.65:
        parts.append(
            "Структуруй ответы: используй подзаголовки и списки там, где это помогает быстро считывать смысл."
        )
    elif profile.structure_density < 0.35:
        parts.append(
            "Можно отвечать цельным текстом, без избытка списков, главное — логика и плавность."
        )
    else:
        parts.append(
            "Комбинируй абзацы и короткие списки так, чтобы текст был и живым, и читаемым."
        )

    if profile.explanation_depth < 0.35:
        parts.append(
            "Даёшь суть кратко: 2–4 абзаца или список до 7 пунктов, без повторов и воды."
        )
    elif profile.explanation_depth > 0.7:
        parts.append(
            "Пользователь нормально воспринимает развёрнутые ответы — можно углубляться, но держи структуру."
        )
    else:
        parts.append(
            "Держи баланс: достаточно деталей, чтобы было понятно, но без перегруза техническими тонкостями."
        )

    if profile.fire_level > 0.7:
        parts.append(
            "Можно быть довольно прямым и жёстким, но не переходи на личности и не используй агрессию."
        )
    elif profile.fire_level < 0.25:
        parts.append(
            "Формулируй мягко и поддерживающе, без морализаторства и давления, особенно в личных темах."
        )
    else:
        parts.append(
            "Позволяй себе честную прямоту, но обрамляй её в уважительный и конструктивный тон."
        )

    return " ".join(parts)


def style_profile_to_summary(profile: StyleProfile) -> str:
    addr = "общение на «Вы»" if profile.address == "vy" else "общение на «ты»"

    if profile.formality > 0.7:
        frm = "деловой, аккуратный тон"
    elif profile.formality < 0.3:
        frm = "разговорный, свободный тон"
    else:
        frm = "нейтральный стиль общения"

    if profile.structure_density > 0.65:
        struct = "любит списки и чёткую структуру"
    elif profile.structure_density < 0.35:
        struct = "чаще пишет «полотном» без жёстких списков"
    else:
        struct = "комбинирует абзацы и списки по ситуации"

    if profile.explanation_depth < 0.35:
        depth = "предпочитает, когда всё максимально кратко"
    elif profile.explanation_depth > 0.7:
        depth = "нормально воспринимает развёрнутые объяснения"
    else:
        depth = "оптимален средний уровень деталей"

    if profile.fire_level > 0.7:
        fire = "можно говорить довольно жёстко и прямо"
    elif profile.fire_level < 0.25:
        fire = "важна бережная, мягкая подача"
    else:
        fire = "честность окей, но без перегибов"

    return f"{addr}; {frm}; {struct}; {depth}; {fire}."


def describe_communication_style(telegram_id: int) -> str:
    profile = _load_style_profile(telegram_id)
    if profile:
        return style_profile_to_summary(profile)

    texts = get_recent_user_messages(telegram_id, limit=30)
    if not texts:
        return "Пока мало данных — подстраиваюсь под тебя по ходу диалога."

    joined = " ".join(texts)
    total_len = sum(len(t) for t in texts if t)
    avg_len = total_len / max(len(texts), 1)

    if avg_len < 80:
        length_desc = "короткие, ёмкие сообщения"
    elif avg_len < 220:
        length_desc = "средний объём без перегруза"
    else:
        length_desc = "развёрнутые, подробные сообщения"

    lower = joined.lower()
    formal_markers = ["здравствуйте", "добрый день", "добрый вечер", "уважаем", "будьте добры"]
    uses_vy = any(m in lower for m in formal_markers) or " вы " in lower
    tone_desc = (
        "общение на «Вы», аккуратный тон"
        if uses_vy
        else "общение на «ты», живой и прямой тон"
    )

    if any(ch in joined for ch in ["\n- ", "\n•", "1.", "2)"]):
        struct_desc = "любишь структуру и списки"
    else:
        struct_desc = "чаще используешь свободный формат без жёсткой структуры"

    return f"{length_desc}; {tone_desc}; {struct_desc}."


def build_style_hint(telegram_id: int) -> str:
    profile = build_style_profile_from_history(telegram_id)
    return style_profile_to_hint(profile)


# ---------------------------------------------------------------------------
# Интенты и эмоции
# ---------------------------------------------------------------------------

def detect_intent(user_text: str) -> str:
    text = (user_text or "").lower()

    plan_keywords = ["план", "по шагам", "roadmap", "чек-лист", "чеклист", "структурируй"]
    if any(k in text for k in plan_keywords):
        return "plan"

    brainstorm_keywords = [
        "идеи",
        "варианты",
        "мозговой штурм",
        "brainstorm",
        "нейминг",
        "название",
        "как назвать",
    ]
    if any(k in text for k in brainstorm_keywords):
        return "brainstorm"

    emotional_keywords = [
        "мне плохо",
        "плохо на душе",
        "тревога",
        "тревожно",
        "страшно",
        "выгорел",
        "выгорание",
        "нет сил",
        "устал",
        "мотивация",
    ]
    if any(k in text for k in emotional_keywords):
        return "emotional"

    analysis_keywords = [
        "проанализируй",
        "анализ",
        "разбор",
        "почему",
        "объясни",
        "разложи",
    ]
    if any(k in text for k in analysis_keywords) or len(text) > 600:
        return "analysis"

    return "other"


def detect_emotion(user_text: str) -> str:
    text = (user_text or "").lower()

    anger_keys = ["злость", "злюсь", "бесит", "раздражает", "раздражение", "агресс", "кипит"]
    if any(k in text for k in anger_keys):
        return "anger"

    overload_keys = [
        "перегруз",
        "перегружен",
        "слишком много",
        "не успеваю",
        "завал",
        "голова не варит",
        "голова кипит",
        "давит",
        "давление задач",
    ]
    if any(k in text for k in overload_keys):
        return "overload"

    anxiety_keys = [
        "тревог",
        "пережива",
        "волнуюсь",
        "боюсь",
        "страшно",
        "нервнича",
        "паник",
    ]
    if any(k in text for k in anxiety_keys):
        return "anxiety"

    apathy_keys = [
        "нет сил",
        "ничего не хочется",
        "апат",
        "пусто внутри",
        "опустились руки",
        "устал жить",
        "выгорел",
        "выгорание",
        "устал до смерти",
    ]
    if any(k in text for k in apathy_keys):
        return "apathy"

    inspired_keys = [
        "вдохнов",
        "кайф",
        "заряжен",
        "огонь",
        "горю идеей",
        "мотивирован",
        "лютый заряд",
    ]
    if any(k in text for k in inspired_keys):
        return "inspired"

    return "neutral"


def build_emotion_hint(emotion: str) -> str:
    if emotion == "overload":
        return (
            "Если в запросе чувствуется перегруз и ощущение завала задач, "
            "отвечай как «холодная голова»: помоги упростить и разгрузить. "
            "Дай 3–5 простых шагов, упорядочь хаос, убери лишние действия. "
            "Не пиши напрямую, что заметил перегруз — просто веди себя спокойнее и структурнее."
        )
    if emotion == "anxiety":
        return (
            "Если в запросе много тревоги или переживаний, отвечай особенно мягко и опорно. "
            "Избегай катастрофизации и страшных формулировок. "
            "Дай 2–4 понятных шага, которые снижают неопределённость. "
            "Можешь предложить очень короткую дыхательную или заземляющую практику (1–2 предложения), "
            "но как опцию, а не как приказ. Не пиши фразу вида «я вижу, что ты тревожишься»."
        )
    if emotion == "anger":
        return (
            "Если чувствуется злость или раздражение, не подливай масла в огонь и не обесценивай эмоции. "
            "Помоги перевести энергию в конструктив: предложи фокус на действиях и конкретных шагах. "
            "Тон — спокойный, без морализаторства и без прямых оценок личности."
        )
    if emotion == "apathy":
        return (
            "Если ощущается апатия или сильная усталость, не дави и не читай нотаций. "
            "Предложи 1–3 очень простых, реалистичных шага, которые дают минимальное движение вперёд "
            "и чувство контроля. Избегай фраз вида «нужно просто взять себя в руки»."
        )
    if emotion == "inspired":
        return (
            "Если пользователь звучит вдохновлённо и заряженно, не тормози его энтузиазм. "
            "Помоги упаковать энергию в понятный план и следующие шаги, чуть структурируй идеи. "
            "Тон может быть более живым и поддерживающим."
        )
    return ""


def build_system_prompt(mode_key: str, intent: str, style_hint: Optional[str]) -> str:
    mode_cfg = MODE_CONFIGS.get(mode_key, MODE_CONFIGS[DEFAULT_MODE_KEY])

    if intent == "plan":
        intent_suffix = (
            "Пользователь ожидает прежде всего чёткий план действий. "
            "Сделай поэтапный план с логичными блоками и краткими пояснениями."
        )
    elif intent == "analysis":
        intent_suffix = (
            "Пользователь просит глубокий разбор. "
            "Разбери ситуацию по шагам: контекст → ключевые факторы → варианты → вывод."
        )
    elif intent == "brainstorm":
        intent_suffix = (
            "Пользователь ждёт мозговой штурм. "
            "Предложи несколько разных подходов и вариантов, сгруппируй их и рядом с каждым "
            "дай короткий комментарий."
        )
    elif intent == "emotional":
        intent_suffix = (
            "Пользователь в эмоциональном запросе. "
            "Сначала аккуратно отзеркаль состояние (без грубых ярлыков), затем предложи простые, "
            "реалистичные шаги без токсичного позитива."
        )
    else:
        intent_suffix = (
            "Формат ответа выбирай исходя из запроса, но всегда держи структуру и ясность мысли."
        )

    parts = [BASE_SYSTEM_PROMPT, mode_cfg.system_suffix, intent_suffix]
    if style_hint:
        parts.append(style_hint)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

async def _call_deepseek(
    user_text: str,
    mode_key: str,
    intent: str,
    style_hint: Optional[str],
    history: Optional[List[Dict[str, str]]],
) -> str:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY is not set")

    sys_prompt = build_system_prompt(mode_key, intent, style_hint)
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }

    messages: List[Dict[str, str]] = [{"role": "system", "content": sys_prompt}]
    if history:
        messages.extend(history[-10:])
    messages.append({"role": "user", "content": user_text})

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": 0.7,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(DEEPSEEK_API_URL, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()

    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"DeepSeek empty response: {data}")
    return (choices[0]["message"]["content"] or "").strip()


async def _call_groq(
    user_text: str,
    mode_key: str,
    intent: str,
    style_hint: Optional[str],
    history: Optional[List[Dict[str, str]]],
) -> str:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not set")

    sys_prompt = build_system_prompt(mode_key, intent, style_hint)
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    messages: List[Dict[str, str]] = [{"role": "system", "content": sys_prompt}]
    if history:
        messages.extend(history[-10:])
    messages.append({"role": "user", "content": user_text})

    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0.7,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(GROQ_API_URL, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()

    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"Groq empty response: {data}")
    return (choices[0]["message"]["content"] or "").strip()


async def generate_ai_reply(
    user_text: str,
    mode_key: str,
    style_hint: Optional[str] = None,
    history: Optional[List[Dict[str, str]]] = None,
) -> str:
    intent = detect_intent(user_text)
    last_error: Optional[Exception] = None

    if DEEPSEEK_API_KEY:
        try:
            return await _call_deepseek(user_text, mode_key, intent, style_hint, history)
        except Exception as e:  # noqa: BLE001
            last_error = e
            logger.exception("DeepSeek API error: %r", e)

    if GROQ_API_KEY:
        try:
            return await _call_groq(user_text, mode_key, intent, style_hint, history)
        except Exception as e:  # noqa: BLE001
            last_error = e
            logger.exception("Groq API error: %r", e)

    if last_error:
        return (
            "⚠️ Что-то пошло не так при обращении к ИИ.\n"
            "Попробуй повторить запрос чуть позже."
        )

    return (
        "⚠️ ИИ-модель сейчас не настроена.\n"
        "Проверь конфигурацию сервера бота."
    )


# ---------------------------------------------------------------------------
# Engine: чистый мозг
# ---------------------------------------------------------------------------

@dataclass
class EngineAnswer:
    text: str
    use_stream: bool = False
    meta: Dict[str, Any] = field(default_factory=dict)


class Engine:
    """
    Telegram ничего не знает про этот класс.

    Интерфейс:
      handle_message(user_id: int, text: str, mode_key: str) -> EngineAnswer
    """

    async def handle_message(self, telegram_id: int, text: str, mode_key: str) -> EngineAnswer:
        text = (text or "").strip()
        if not text:
            return EngineAnswer(text="", use_stream=False)

        # 1) сохраняем сообщение пользователя в историю
        save_message(telegram_id, "user", text)

        # 2) стиль + эмоции
        base_style_hint = build_style_hint(telegram_id)

        emotion = detect_emotion(text)
        emotion_hint = build_emotion_hint(emotion)
        if emotion_hint:
            style_hint = f"{base_style_hint}\n\n{emotion_hint}"
        else:
            style_hint = base_style_hint

        history = get_recent_dialog_history(telegram_id, limit=12)

        # 3) быстрый vs глубокий режим
        length = len(text)
        if length < 120:
            style_hint = (
                (style_hint + "\n\n") if style_hint else ""
            ) + (
                "Запрос короткий. Сделай ответ компактным (2–4 абзаца или список до 7 пунктов). "
                "В конце одной строкой предложи при необходимости «Раскрой подробнее»."
            )
            use_stream = False
        else:
            style_hint = (
                (style_hint + "\n\n") if style_hint else ""
            ) + (
                "Запрос объёмный. Дай глубокий, хорошо структурированный разбор с подзаголовками и выводом."
            )
            use_stream = True

        # 4) LLM
        reply = await generate_ai_reply(
            user_text=text,
            mode_key=mode_key,
            style_hint=style_hint,
            history=history,
        )

        # 5) сохраняем ответ ассистента
        save_message(telegram_id, "assistant", reply)

        intent = detect_intent(text)

        return EngineAnswer(
            text=reply,
            use_stream=use_stream,
            meta={"emotion": emotion, "intent": intent},
        )
