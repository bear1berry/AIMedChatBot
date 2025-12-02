from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import httpx

from .modes import build_system_prompt, DEFAULT_MODE_KEY

logger = logging.getLogger(__name__)

# === YandexGPT / OpenAI-совместимый API ===
#
# Бот работает через OpenAI-совместимый endpoint YandexGPT:
#   https://llm.api.cloud.yandex.net/v1/chat/completions
#
# Нужны:
#   - сервисный API-ключ:  YANDEX_CLOUD_API_KEY
#   - ID каталога:         YANDEX_CLOUD_FOLDER
#
# В .env нужно добавить, например:
#   YANDEX_CLOUD_API_KEY=yc_sa_********************************
#   YANDEX_CLOUD_FOLDER=b1gxxxxxxxxxxxxxxxx

YANDEX_CLOUD_FOLDER = os.getenv("YANDEX_CLOUD_FOLDER", "").strip()

# Оставляем AIML_API_KEY для обратной совместимости, но приоритет у YANDEX_CLOUD_API_KEY.
AIML_API_KEY = (
    os.getenv("YANDEX_CLOUD_API_KEY") or os.getenv("AIML_API_KEY", "")
).strip()

AIML_API_URL = os.getenv(
    "AIML_API_URL",
    "https://llm.api.cloud.yandex.net/v1/chat/completions",
).strip()


def _default_model_uri(short_id: str) -> str:
    """
    Собираем URI модели вида gpt://<folder>/<short_id>.

    Если каталог не указан (YANDEX_CLOUD_FOLDER пустой),
    возвращаем short_id как есть — тогда его можно задать
    полностью через переменную окружения AIML_MODEL_*.
    """
    if YANDEX_CLOUD_FOLDER:
        if short_id.startswith("gpt://"):
            return short_id
        return f"gpt://{YANDEX_CLOUD_FOLDER}/{short_id}"
    return short_id


# Основные модели YandexGPT
AIML_MODEL_PRIMARY = os.getenv(
    "AIML_MODEL_PRIMARY",
    _default_model_uri("yandexgpt/latest"),
)
AIML_MODEL_FAST = os.getenv(
    "AIML_MODEL_FAST",
    _default_model_uri("yandexgpt-lite/latest"),
)

# Дополнительные "профили" — для совместимости со старой логикой.
# Можно переопределить через переменные окружения, если у вас есть другие модели.
AIML_MODEL_GPT_OSS_120B = os.getenv(
    "AIML_MODEL_GPT_OSS_120B",
    AIML_MODEL_PRIMARY,
)
AIML_MODEL_DEEPSEEK_REASONER = os.getenv(
    "AIML_MODEL_DEEPSEEK_REASONER",
    _default_model_uri("yandexgpt/rc"),
)
AIML_MODEL_DEEPSEEK_CHAT = os.getenv(
    "AIML_MODEL_DEEPSEEK_CHAT",
    AIML_MODEL_FAST,
)

# Лимиты на пользователя (можно переопределить через переменные окружения)
RATE_LIMIT_PER_MINUTE = int(os.getenv("AIMED_RATE_LIMIT_PER_MINUTE", "20"))
RATE_LIMIT_PER_DAY = int(os.getenv("AIMED_RATE_LIMIT_PER_DAY", "200"))


# === Rate limiting ===


class RateLimitError(Exception):
    """Исключение, когда превышен лимит запросов на пользователя."""

    def __init__(self, scope: str) -> None:
        # scope: "minute" или "day"
        super().__init__(scope)
        self.scope = scope


@dataclass
class _RateLimitBucket:
    minute_ts: int = 0
    minute_count: int = 0
    day_ts: int = 0
    day_count: int = 0


_rate_limits: Dict[int, _RateLimitBucket] = {}


def _check_rate_limit(user_id: int) -> None:
    now = int(time.time())
    minute = now // 60
    day = now // 86400

    bucket = _rate_limits.get(user_id)
    if bucket is None:
        bucket = _RateLimitBucket()
        _rate_limits[user_id] = bucket

    if bucket.minute_ts != minute:
        bucket.minute_ts = minute
        bucket.minute_count = 0

    if bucket.day_ts != day:
        bucket.day_ts = day
        bucket.day_count = 0

    if bucket.minute_count >= RATE_LIMIT_PER_MINUTE:
        raise RateLimitError("minute")

    if bucket.day_count >= RATE_LIMIT_PER_DAY:
        raise RateLimitError("day")

    bucket.minute_count += 1
    bucket.day_count += 1


# === Workspaces & conversation state ===


@dataclass
class Workspace:
    id: str
    title: str
    mode_key: str = DEFAULT_MODE_KEY
    # auto | gpt4 | mini | oss | deepseek_reasoner | deepseek_chat
    model_profile: str = "auto"
    messages: List[dict] = field(default_factory=list)


def _default_workspace_title(ws_id: str) -> str:
    if ws_id == "default":
        return "AI Universal"
    if ws_id == "study":
        return "Study Room"
    if ws_id == "channel":
        return "AI Medicine / канал"
    if ws_id == "personal":
        return "Личное / жизнь"
    return f"Workspace {ws_id}"


@dataclass
class ConversationState:
    current_workspace_id: str = "default"
    workspaces: Dict[str, Workspace] = field(default_factory=dict)

    @property
    def current(self) -> Workspace:
        if self.current_workspace_id not in self.workspaces:
            # ленивая инициализация
            self.workspaces[self.current_workspace_id] = Workspace(
                id=self.current_workspace_id,
                title=_default_workspace_title(self.current_workspace_id),
            )
        return self.workspaces[self.current_workspace_id]

    # Backward-compatible properties

    @property
    def mode_key(self) -> str:
        return self.current.mode_key

    @mode_key.setter
    def mode_key(self, value: str) -> None:
        self.current.mode_key = value

    @property
    def model_profile(self) -> str:
        return self.current.model_profile

    @model_profile.setter
    def model_profile(self, value: str) -> None:
        self.current.model_profile = value

    @property
    def messages(self) -> List[dict]:
        return self.current.messages

    @messages.setter
    def messages(self, value: List[dict]) -> None:
        self.current.messages = value


_conversations: Dict[int, ConversationState] = {}


def _ensure_default_workspaces(state: ConversationState) -> None:
    """Создаём дефолтные пространства, если их ещё нет."""
    if state.workspaces:
        return

    state.workspaces["default"] = Workspace(
        id="default",
        title=_default_workspace_title("default"),
        mode_key=DEFAULT_MODE_KEY,
        model_profile="auto",
    )
    # Дополнительные преднастроенные пространства
    state.workspaces["channel"] = Workspace(
        id="channel",
        title=_default_workspace_title("channel"),
        mode_key="content_creator",
        model_profile="auto",
    )
    state.workspaces["study"] = Workspace(
        id="study",
        title=_default_workspace_title("study"),
        mode_key=DEFAULT_MODE_KEY,
        model_profile="auto",
    )
    state.workspaces["personal"] = Workspace(
        id="personal",
        title=_default_workspace_title("personal"),
        mode_key="friendly_chat",
        model_profile="auto",
    )
    state.current_workspace_id = "default"


def get_state(user_id: int) -> ConversationState:
    state = _conversations.get(user_id)
    if state is None:
        state = ConversationState()
        _conversations[user_id] = state
    _ensure_default_workspaces(state)
    return state


def list_workspaces(user_id: int) -> List[Workspace]:
    state = get_state(user_id)
    return list(state.workspaces.values())


def get_current_workspace(user_id: int) -> Workspace:
    state = get_state(user_id)
    return state.current


def set_current_workspace(user_id: int, workspace_id: str) -> Workspace:
    state = get_state(user_id)
    if workspace_id not in state.workspaces:
        state.workspaces[workspace_id] = Workspace(
            id=workspace_id,
            title=_default_workspace_title(workspace_id),
        )
    state.current_workspace_id = workspace_id
    return state.current


def create_workspace(
    user_id: int,
    title: Optional[str] = None,
    base_mode: Optional[str] = None,
    model_profile: Optional[str] = None,
) -> Workspace:
    state = get_state(user_id)
    # подберём свободный id wsN
    idx = 1
    while True:
        ws_id = f"ws{idx}"
        if ws_id not in state.workspaces:
            break
        idx += 1

    ws = Workspace(
        id=ws_id,
        title=title.strip() if title and title.strip() else _default_workspace_title(ws_id),
        mode_key=base_mode or DEFAULT_MODE_KEY,
        model_profile=model_profile or "auto",
    )
    state.workspaces[ws_id] = ws
    state.current_workspace_id = ws_id
    return ws


def set_mode(user_id: int, mode_key: str) -> ConversationState:
    """
    Установить режим для текущего workspace и очистить историю.
    """
    state = get_state(user_id)
    state.mode_key = mode_key
    state.messages.clear()
    return state


def reset_state(user_id: int) -> None:
    """
    Очистить историю только текущего workspace, режим и модель оставить.
    """
    state = _conversations.get(user_id)
    if state:
        state.current.messages.clear()


_MODEL_PROFILE_LABELS = {
    "auto": "Авто (подбор)",
    "gpt4": "YandexGPT (основная)",
    "mini": "YandexGPT Lite (быстрее)",
    "oss": "Экспериментальный профиль",
    "deepseek_reasoner": "Режим рассуждений (YandexGPT)",
    "deepseek_chat": "Диалоговый режим (YandexGPT)",
}


def set_model_profile(user_id: int, profile: str) -> ConversationState:
    """
    Установить профиль модели для текущего workspace.
    """
    if profile not in _MODEL_PROFILE_LABELS:
        raise ValueError(f"Unknown model profile: {profile}")
    state = get_state(user_id)
    state.model_profile = profile
    return state


def get_model_profile_label(profile: str) -> str:
    return _MODEL_PROFILE_LABELS.get(profile, "Авто (подбор)")


# === Выбор модели и пост-обработка ===


def _postprocess_reply(text: str) -> str:
    """
    Лёгкая пост-обработка ответа: убираем лишние пробелы и дублирующиеся пустые строки.
    """
    text = text.replace("\r\n", "\n").strip()
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text


def _model_human_name(model_id: str) -> str:
    if model_id == AIML_MODEL_PRIMARY:
        return "YandexGPT"
    if model_id == AIML_MODEL_FAST:
        return "YandexGPT Lite"
    if model_id == AIML_MODEL_GPT_OSS_120B:
        return "YandexGPT Experimental"
    if model_id == AIML_MODEL_DEEPSEEK_REASONER:
        return "YandexGPT Reasoning"
    if model_id == AIML_MODEL_DEEPSEEK_CHAT:
        return "YandexGPT Chat"
    return "LLM"


def _model_emoji(model_id: str) -> str:
    if model_id == AIML_MODEL_PRIMARY:
        return "🧠"
    if model_id == AIML_MODEL_FAST:
        return "⚡️"
    if model_id == AIML_MODEL_GPT_OSS_120B:
        return "🧪"
    if model_id == AIML_MODEL_DEEPSEEK_REASONER:
        return "🧩"
    if model_id == AIML_MODEL_DEEPSEEK_CHAT:
        return "💬"
    return "🤖"


def _model_short_desc(model_id: str) -> str:
    """
    Краткое описание модели для подписи перед ответом.
    """
    if model_id == AIML_MODEL_PRIMARY:
        return "точная и универсальная модель YandexGPT"
    if model_id == AIML_MODEL_FAST:
        return "быстрые ответы и черновики (Lite)"
    if model_id == AIML_MODEL_GPT_OSS_120B:
        return "экспериментальный профиль на базе YandexGPT"
    if model_id == AIML_MODEL_DEEPSEEK_REASONER:
        return "режим усиленного рассуждения"
    if model_id == AIML_MODEL_DEEPSEEK_CHAT:
        return "диалоговый режим"
    return "LLM"


def _is_reasoning_task(question: str) -> bool:
    q = question.lower()
    return any(
        word in q
        for word in [
            "почему",
            "обоснуй",
            "объясни ход мыслей",
            "разбери кейс",
            "задача",
            "кейс",
        ]
    )


def _is_brainstorm_task(question: str) -> bool:
    q = question.lower()
    return any(
        word in q
        for word in [
            "идея",
            "идеи",
            "варианты",
            "мозговой штурм",
            "придумай",
            "концепцию",
        ]
    )


def _is_code_task(question: str) -> bool:
    q = question.lower()
    return any(
        word in q
        for word in [
            "код",
            "python",
            "sql",
            "javascript",
            "ошибка",
            "traceback",
            "програм",
            "скрипт",
        ]
    )


def _select_models_for_query(question: str, state: ConversationState) -> List[str]:
    """
    Возвращает список id моделей, которые нужно дернуть для ответа.
    Если выбрана ручная модель — всегда одна.
    В режиме auto — 1–2 модели в зависимости от задачи.
    """
    profile = state.model_profile

    # Ручной выбор — всегда ровно одна модель
    if profile == "gpt4":
        return [AIML_MODEL_PRIMARY]
    if profile == "mini":
        return [AIML_MODEL_FAST]
    if profile == "oss":
        return [AIML_MODEL_GPT_OSS_120B]
    if profile == "deepseek_reasoner":
        return [AIML_MODEL_DEEPSEEK_REASONER]
    if profile == "deepseek_chat":
        return [AIML_MODEL_DEEPSEEK_CHAT]

    # Авто-подбор
    is_reasoning = _is_reasoning_task(question)
    is_brainstorm = _is_brainstorm_task(question)
    is_code = _is_code_task(question)

    # Сложные кейсы / код — 2 модели: основная + reasoning
    if is_reasoning or is_code:
        return [AIML_MODEL_PRIMARY, AIML_MODEL_DEEPSEEK_REASONER]

    # Брейншторм / креатив — "экспериментальная" + основная
    if is_brainstorm:
        return [AIML_MODEL_GPT_OSS_120B, AIML_MODEL_PRIMARY]

    # Обычный короткий вопрос — быстрая модель
    if len(question) < 400:
        return [AIML_MODEL_FAST]

    # Остальное — основная модель
    return [AIML_MODEL_PRIMARY]


# === Низкоуровневый вызов YandexGPT ===


async def _call_model(model: str, messages: List[dict]) -> str:
    """
    Вызов YandexGPT (OpenAI-совместимый endpoint) для одной модели.
    Работает через HTTP-запрос к https://llm.api.cloud.yandex.net/v1/chat/completions.
    """
    if not AIML_API_KEY:
        raise RuntimeError("YANDEX_CLOUD_API_KEY (или AIML_API_KEY) не задан")

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.7,
        "top_p": 1.0,
        "max_tokens": 2048,
    }

    headers = {
        "Authorization": f"Bearer {AIML_API_KEY}",
        "Content-Type": "application/json",
    }
    # Для Yandex OpenAI-совместимого API нужно указать каталог
    # через заголовок OpenAI-Project.
    if YANDEX_CLOUD_FOLDER:
        headers["OpenAI-Project"] = YANDEX_CLOUD_FOLDER

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(AIML_API_URL, json=payload, headers=headers)

    try:
        data = resp.json()
    except Exception:
        logger.exception("Failed to parse YandexGPT response: %s", resp.text[:500])
        raise RuntimeError("Failed to parse YandexGPT response")

    if resp.status_code >= 400:
        err = data.get("error") if isinstance(data, dict) else data
        logger.error("YandexGPT error (%s): %r", resp.status_code, err)
        raise RuntimeError(f"YandexGPT error {resp.status_code}: {err}")

    try:
        content = data["choices"][0]["message"]["content"]
    except Exception:
        logger.exception("Unexpected YandexGPT payload: %r", data)
        raise RuntimeError("Unexpected YandexGPT response format")

    return _postprocess_reply(content)


# === Публичный API для обработчиков ===


async def ask_ai(user_id: int, text: str, user_name: Optional[str] = None) -> str:
    """
    Главная точка входа: отправить запрос в ИИ
    с учётом workspace, режима и истории.
    """
    _check_rate_limit(user_id)

    state = get_state(user_id)
    ws = state.current

    system_prompt = build_system_prompt(mode_key=ws.mode_key, user_name=user_name)
    messages: List[dict] = [{"role": "system", "content": system_prompt}]
    messages.extend(ws.messages)
    messages.append({"role": "user", "content": text})

    models = _select_models_for_query(text, state)

    if len(models) == 1:
        reply = await _call_model(models[0], messages)
    else:
        # Параллельно дёргаем несколько моделей и собираем единый ответ
        tasks = [asyncio.create_task(_call_model(m, messages)) for m in models]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        blocks: List[str] = []
        for model_id, result in zip(models, results):
            name = _model_human_name(model_id)
            emoji = _model_emoji(model_id)
            desc = _model_short_desc(model_id)

            if isinstance(result, Exception):
                logger.exception("Model %s failed", model_id, exc_info=result)
                block = (
                    f"{emoji} <b>{name}</b> ({desc}):\n"
                    "⚠️ Ошибка при обращении к модели. Попробуй ещё раз."
                )
            else:
                block = f"{emoji} <b>{name}</b> ({desc}):\n{result}"

            blocks.append(block)

        reply = "\n\n━━━━━━━━━━━━━━\n\n".join(blocks)

    # Обновляем историю текущего workspace
    ws.messages.append({"role": "user", "content": text})
    ws.messages.append({"role": "assistant", "content": reply})

    # ограничиваем историю (последние N обменов)
    max_turns = 12
    if len(ws.messages) > max_turns * 2:
        ws.messages = ws.messages[-max_turns * 2 :]

    return reply


async def healthcheck_llm() -> bool:
    """
    Лёгкий пинг для проверки доступности модели.
    """
    try:
        _ = await _call_model(AIML_MODEL_FAST, [{"role": "user", "content": "ping"}])
        return True
    except Exception:
        logger.exception("LLM healthcheck failed")
        return False
