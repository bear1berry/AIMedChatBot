from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List

from groq import Groq

from .modes import build_system_prompt, DEFAULT_MODE_KEY

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME", "openai/gpt-oss-120b")

RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "20"))
RATE_LIMIT_PER_DAY = int(os.getenv("RATE_LIMIT_PER_DAY", "500"))

if not GROQ_API_KEY:
    logger.warning("GROQ_API_KEY is not set. LLM calls will fail.")

_client = Groq(api_key=GROQ_API_KEY)


@dataclass
class ConversationState:
    mode_key: str = DEFAULT_MODE_KEY
    messages: List[dict] = field(default_factory=list)


_conversations: Dict[int, ConversationState] = {}


@dataclass
class RateBucket:
    minute_ts: int = 0
    minute_count: int = 0
    day_ts: int = 0
    day_count: int = 0


_rate_buckets: Dict[int, RateBucket] = {}


class RateLimitError(Exception):
    """Raised when per-user rate limit is exceeded."""

    def __init__(self, scope: str):
        # scope: "minute" or "day"
        super().__init__(scope)
        self.scope = scope


def _check_rate_limit(user_id: int) -> None:
    now = int(time.time())
    minute = now // 60
    day = now // (24 * 60 * 60)

    bucket = _rate_buckets.get(user_id)
    if bucket is None:
        bucket = RateBucket(minute_ts=minute, day_ts=day)
        _rate_buckets[user_id] = bucket

    # reset windows if needed
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


def get_state(user_id: int) -> ConversationState:
    state = _conversations.get(user_id)
    if state is None:
        state = ConversationState()
        _conversations[user_id] = state
    return state


def set_mode(user_id: int, mode_key: str) -> ConversationState:
    """
    Set chat mode for the user and clear history for a clean start.
    """
    state = get_state(user_id)
    state.mode_key = mode_key
    state.messages.clear()
    return state


def reset_state(user_id: int) -> None:
    """
    Clear only the history for the user but keep the current mode.
    """
    state = _conversations.get(user_id)
    if state:
        state.messages.clear()


async def ask_ai(user_id: int, text: str, user_name: str | None = None) -> str:
    """
    Main entry point: send user message to LLM and get assistant reply.
    Conversation history and mode are handled automatically.
    """
    _check_rate_limit(user_id)

    state = get_state(user_id)
    system_prompt = build_system_prompt(mode_key=state.mode_key, user_name=user_name)

    # Build messages: system + history + new user message
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(state.messages)
    messages.append({"role": "user", "content": text})

    try:
        completion = _client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            temperature=0.7,
            top_p=1,
            max_completion_tokens=2048,
            reasoning_effort="medium",
        )
    except Exception:
        logger.exception("Error while calling Groq ChatCompletion")
        raise

    reply = completion.choices[0].message.content or ""

    # update state history
    state.messages.append({"role": "user", "content": text})
    state.messages.append({"role": "assistant", "content": reply})

    # truncate history (keep last N turns)
    max_turns = 12  # user+assistant pair = 2 messages
    if len(state.messages) > max_turns * 2:
        state.messages = state.messages[-max_turns * 2 :]

    return reply


async def healthcheck_llm() -> bool:
    """
    Lightweight ping to check model availability.
    """
    try:
        _ = _client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": "ping"}],
            max_completion_tokens=1,
            temperature=0.0,
        )
        return True
    except Exception:
        logger.exception("LLM healthcheck failed")
        return False
