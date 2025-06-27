"""
Deepgram HTTP Client - low-latency \u00b5-law streaming to Twilio
-----------------------------------------------------------------

Solicita TTS al endpoint Speak de Deepgram y lo env\u00eda a Twilio Media Streams
simulando reproducci\u00f3n en tiempo real.

- Frames de 20 ms (160 bytes @ 8 kHz \u00b5-law)
- Agrupa m\u00e1x. 5 frames (100 ms) por paquete
- Mantiene el pre-buffer \u2264 200 ms para evitar *buffer_overrun*

Credenciales mediante la variable de entorno ``DEEPGRAM_KEY``.
"""

from __future__ import annotations

import os
import base64
import time
import json
import asyncio
import logging
from io import BytesIO
from typing import Callable, Awaitable

import audioop  # type: ignore
import requests

DEEPGRAM_KEY = os.environ["DEEPGRAM_KEY"]
DEEPGRAM_TTS_MODEL = os.getenv("DEEPGRAM_TTS_MODEL", "aura-2-estrella-es")

FRAME_SIZE = 160  # 20 ms @ 8 kHz \u00b5-law
GROUP_FRAMES = 5
MAX_AHEAD_MS = 200
GAIN = 1

logger = logging.getLogger("deepgram_http_client")

WebSocketSend = Callable[[str], Awaitable[None]]


async def send_deepgram_tts_to_twilio(
    text: str,
    stream_sid: str,
    websocket_send: WebSocketSend,
    *,
    model: str = DEEPGRAM_TTS_MODEL,
    group_frames: int = GROUP_FRAMES,
    max_ahead_ms: int = MAX_AHEAD_MS,
    gain: float = GAIN,
) -> None:
    """Genera TTS en Deepgram y lo gotea hacia Twilio."""

    logger.info("\U0001f50a Solicitando TTS a Deepgram…")

    url = "https://api.deepgram.com/v1/speak"
    params = {
        "model": model,
        "encoding": "mulaw",
        "sample_rate": "8000",
    }
    headers = {
        "Authorization": f"Token {DEEPGRAM_KEY}",
        "Accept": "audio/mulaw",
    }
    payload = {"text": text}

    try:
        t_request = time.perf_counter()
        response = requests.post(
            url,
            params=params,
            json=payload,
            headers=headers,
            stream=True,
            timeout=120,
        )
        response.raise_for_status()

        first_chunk_at: float | None = None
        buffer = BytesIO()
        for chunk in response.iter_content(chunk_size=4096):
            if not chunk:
                continue
            if first_chunk_at is None:
                first_chunk_at = time.perf_counter()
                logger.info("\u23f1\ufe0f Deepgram primer chunk tras %.1f ms", (first_chunk_at - t_request) * 1000)
            buffer.write(chunk)
        audio_raw: bytes = buffer.getvalue()
    except Exception as exc:
        logger.error("\ud83d\udea8 Error solicitando TTS a Deepgram: %s", exc)
        await _safe_send_mark(websocket_send, stream_sid, "error")
        return

    if audio_raw.startswith(b"RIFF"):
        logger.warning("\u26a0\ufe0f Deepgram devolvi\u00f3 WAV; quitando cabecera de 44 bytes")
        audio_raw = audio_raw[44:]

    try:
        if gain != 1.0:
            audio_raw = audioop.mul(audio_raw, 1, gain)
    except Exception as exc:
        logger.warning("\u274c Error al amplificar audio: %s", exc)

    if not audio_raw:
        logger.error("\ud83d\udea8 Deepgram devolvi\u00f3 audio vac\u00edo")
        await _safe_send_mark(websocket_send, stream_sid, "error")
        return

    total_frames = (len(audio_raw) + FRAME_SIZE - 1) // FRAME_SIZE
    logger.info("\u2705 Audio TTS recibido (%d bytes \u2192 %d frames)", len(audio_raw), total_frames)

    start_t = time.perf_counter()
    ts_send_start = start_t
    frame_len = FRAME_SIZE
    idx = 0
    try:
        while idx < total_frames:
            frames_left = total_frames - idx
            frames_this_round = min(group_frames, frames_left)
            start = idx * frame_len
            end = min(len(audio_raw), start + frames_this_round * frame_len)
            chunk = audio_raw[start:end]
            if len(chunk) % frame_len:
                pad = frame_len - (len(chunk) % frame_len)
                chunk += b"\xff" * pad
            now = time.perf_counter()
            elapsed_ms = (now - start_t) * 1000
            audio_time_ms = idx * 20
            ahead_ms = audio_time_ms - elapsed_ms
            if ahead_ms > max_ahead_ms:
                sleep_needed = (ahead_ms - max_ahead_ms) / 1000
                if sleep_needed > 0:
                    await asyncio.sleep(sleep_needed)
            payload64 = base64.b64encode(chunk).decode()
            try:
                await websocket_send(json.dumps({
                    "event": "media",
                    "streamSid": stream_sid,
                    "media": {"payload": payload64},
                }))
            except Exception as ws_exc:
                logger.warning("\u26a0\ufe0f websocket_send fall\u00f3: %s", ws_exc)
                await _safe_send_mark(websocket_send, stream_sid, "error")
                return
            idx += frames_this_round
            await asyncio.sleep(0)
    finally:
        envio_ms = (time.perf_counter() - ts_send_start) * 1000
        logger.info("\ud83d\udcf6 Audio enviado a Twilio en %.1f ms", envio_ms)

    await _safe_send_mark(websocket_send, stream_sid, "end_of_tts")
    logger.info("\ud83c\udfc1 Audio completo enviado a Twilio.")


async def _safe_send_mark(send: WebSocketSend, stream_sid: str, name: str) -> None:
    try:
        await send(json.dumps({
            "event": "mark",
            "streamSid": stream_sid,
            "mark": {"name": name},
        }))
    except Exception as exc:
        logger.debug("(ignorado) No se pudo enviar mark '%s': %s", name, exc)
