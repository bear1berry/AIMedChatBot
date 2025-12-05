import logging
from pathlib import Path
from typing import Optional

import httpx

from bot.config import (
    AUDIO_PROVIDER,
    YANDEX_SPEECHKIT_API_KEY,
    YANDEX_FOLDER_ID,
    BASE_DIR,
)

log = logging.getLogger(__name__)

# SpeechKit REST v1 endpoints
YANDEX_STT_URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
YANDEX_TTS_URL = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"


def _check_yandex_conf() -> None:
    if not YANDEX_SPEECHKIT_API_KEY:
        raise RuntimeError(
            "YANDEX_SPEECHKIT_API_KEY is not set in environment (.env). "
            "SpeechKit is not configured."
        )


async def _yandex_stt(file_path: Path) -> str:
    """
    Распознавание речи через Yandex SpeechKit STT (API v1, sync).
    Telegram voice = OggOpus, SpeechKit его понимает напрямую.
    Ограничения: до ~30 сек и 1 МБ. 
    """
    _check_yandex_conf()

    headers = {
        "Authorization": f"Api-Key {YANDEX_SPEECHKIT_API_KEY}",
    }

    params = {
        "lang": "ru-RU",
        # формат файла: OggOpus (как у voice в Telegram)
        "format": "oggopus",
    }

    if YANDEX_FOLDER_ID:
        params["folderId"] = YANDEX_FOLDER_ID

    log.info("[STT] Sending file %s to Yandex STT", file_path)

    async with httpx.AsyncClient(timeout=40.0) as client:
        with file_path.open("rb") as f:
            resp = await client.post(
                YANDEX_STT_URL,
                params=params,
                content=f.read(),
                headers=headers,
            )

    if resp.status_code != 200:
        log.error("[STT] HTTP %s: %s", resp.status_code, resp.text)
        raise RuntimeError(f"SpeechKit STT HTTP error: {resp.status_code}")

    data = resp.json()
    if data.get("error_code"):
        log.error("[STT] API error: %s", data)
        raise RuntimeError(
            f"SpeechKit STT error: {data.get('error_code')}: {data.get('error_message')}"
        )

    result = (data.get("result") or "").strip()
    log.info("[STT] Recognized text: %r", result)
    return result


async def _yandex_tts(text: str, out_path: Path) -> Path:
    """
    Синтез речи через Yandex SpeechKit TTS (API v1).
    Отдаём формат oggopus, чтобы прямо отправлять как voice в Telegram. 
    """
    _check_yandex_conf()

    headers = {
        "Authorization": f"Api-Key {YANDEX_SPEECHKIT_API_KEY}",
    }

    # Важный момент: API v1 ждёт form-data / x-www-form-urlencoded,
    # поэтому используем data=, а не json=
    data = {
        "text": text,
        "lang": "ru-RU",
        "voice": "alena",  # можно поменять голос при желании
        "format": "oggopus",
    }
    if YANDEX_FOLDER_ID:
        data["folderId"] = YANDEX_FOLDER_ID

    log.info("[TTS] Synthesizing speech (%d chars) to %s", len(text), out_path)

    async with httpx.AsyncClient(timeout=40.0) as client:
        resp = await client.post(YANDEX_TTS_URL, data=data, headers=headers)

    if resp.status_code != 200:
        log.error("[TTS] HTTP %s: %s", resp.status_code, resp.text)
        raise RuntimeError(f"SpeechKit TTS HTTP error: {resp.status_code}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(resp.content)

    return out_path


async def speech_to_text(file_path: Path) -> str:
    """
    Внешняя обёртка для STT. Пока реализован только провайдер 'yandex'.
    """
    if AUDIO_PROVIDER != "yandex":
        raise RuntimeError(f"Unsupported AUDIO_PROVIDER={AUDIO_PROVIDER!r}")

    return await _yandex_stt(file_path)


async def text_to_speech(text: str, file_name: Optional[str] = None) -> Path:
    """
    Внешняя обёртка для TTS.
    Возвращает путь до файла с озвучкой в формате OggOpus.
    """
    if AUDIO_PROVIDER != "yandex":
        raise RuntimeError(f"Unsupported AUDIO_PROVIDER={AUDIO_PROVIDER!r}")

    if not file_name:
        file_name = "tts_output.ogg"

    out_dir = BASE_DIR / "tmp" / "tts"
    out_path = out_dir / file_name

    return await _yandex_tts(text, out_path)
