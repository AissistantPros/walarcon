"""
==============================================================
 Eleven HTTP Client ‒ low‑latency μ‑law streaming to Twilio
==============================================================

• Solicita TTS a ElevenLabs (ulaw_8000) y lo envía a Twilio Media
  Streams simulando reproducción en tiempo real.
• Cumple las recomendaciones oficiales de Twilio:     
  ‑ Frames de 20 ms (160 bytes @ 8 kHz μ‑law).     
  ‑ Agrupar máx. 5 frames (100 ms) por paquete.     
  ‑ Mantener el _pre‑buffer_ ≤ 200 ms para evitar *buffer_overrun*.
• Maneja reconexiones y excepciones del WebSocket para registrar
  problemas de red (desconexión, back‑pressure, etc.).
• **Credenciales** se toman de variables de entorno (Render / Docker
  secrets).  Nunca las pongas en el repositorio ;)

Requiere: `requests`, `audioop`, `asyncio`, `logging`.
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

# --------------------------------------------------------------------------
#  Credenciales y configuración (obligatorio en entorno, p.e. Render / .env)
# --------------------------------------------------------------------------
ELEVEN_LABS_API_KEY  = os.environ["ELEVEN_LABS_API_KEY"]
ELEVEN_LABS_VOICE_ID = os.environ["ELEVEN_LABS_VOICE_ID"]

# --------------------------------------------------------------------------
#  Par á metros de audio
# --------------------------------------------------------------------------
FRAME_SIZE         = 160          # 160 bytes  → 20 ms @ 8 kHz μ‑law
GROUP_FRAMES       = 5            # máx. 100 ms por paquete (1‑5 frames)
MAX_AHEAD_MS       = 200          # no enviar >200 ms adelantado al tiempo real
GAIN               = 1.1          # Ganancia de audio (multiplicador)

logger = logging.getLogger("eleven_http_client")

# Type alias para la función que envía texto a Twilio
WebSocketSend = Callable[[str], Awaitable[None]]


async def send_tts_http_to_twilio(
    text: str,
    stream_sid: str,
    websocket_send: WebSocketSend,
    *,
    group_frames: int = GROUP_FRAMES,
    max_ahead_ms: int = MAX_AHEAD_MS,
    gain: float = GAIN,
) -> None:
    """Genera TTS en ElevenLabs y lo *gotea* hacia Twilio.

    Args:
        text: Texto que se convertirá a voz.
        stream_sid: SID del *Media Stream* de Twilio.
        websocket_send: `await`‑able que envía mensajes JSON al WS.
        group_frames: Cuántos frames (20 ms c/u) incluir en cada paquete.
        max_ahead_ms: Cuánto audio máximo adelantado permitimos (jitter
            buffer de Twilio).
        gain: Factor multiplicador de amplitud μ‑law (1.0 = sin cambio).
    """

    logger.info("🗣️ Solicitando TTS a ElevenLabs…")

    url = (
        f"https://api.elevenlabs.io/v1/text-to-speech/"
        f"{ELEVEN_LABS_VOICE_ID}/stream?output_format=ulaw_8000"
    )
    headers = {
        "xi-api-key": ELEVEN_LABS_API_KEY,
        "Accept": "audio/mulaw",
    }
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.75,
            "style": 0.45,
            "use_speaker_boost": True,
            "speed": 1.2,
        },
    }

    # 1️⃣ Descargar audio de ElevenLabs
    try:
        t_request = time.perf_counter()
        response = requests.post(url, json=payload, headers=headers, stream=True, timeout=120)
        response.raise_for_status()

        first_chunk_at: float | None = None
        buffer = BytesIO()

        for chunk in response.iter_content(chunk_size=4096):
            if not chunk:
                continue  # Ignora keep‑alive vacíos
            if first_chunk_at is None:
                first_chunk_at = time.perf_counter()
                logger.info("⏱️ ElevenLabs primer chunk tras %.1f ms", (first_chunk_at - t_request) * 1000)
            buffer.write(chunk)

        audio_raw: bytes = buffer.getvalue()
    except Exception as exc:
        logger.error("🚨 Error solicitando TTS: %s", exc)
        await _safe_send_mark(websocket_send, stream_sid, "error")
        return

    # 2️⃣ WAV → μ‑law crudo (por si acaso)
    if audio_raw.startswith(b"RIFF"):
        logger.warning("⚠️ ElevenLabs devolvió WAV; quitando cabecera de 44 bytes")
        audio_raw = audio_raw[44:]

    # 3️⃣ Ganancia
    try:
        if gain != 1.0:
            audio_raw = audioop.mul(audio_raw, 1, gain)
            logger.info("🔊 Audio amplificado x%.2f", gain)
    except Exception as exc:
        logger.warning("❌ Error al amplificar audio: %s", exc)

    if not audio_raw:
        logger.error("🚨 ElevenLabs devolvió audio vacío")
        await _safe_send_mark(websocket_send, stream_sid, "error")
        return

    total_frames = (len(audio_raw) + FRAME_SIZE - 1) // FRAME_SIZE
    logger.info("✅ Audio TTS recibido (%d bytes → %d frames)", len(audio_raw), total_frames)

    # 4️⃣ Envío *pacing* a Twilio
    start_t = time.perf_counter()
    ts_send_start = start_t

    frame_len = FRAME_SIZE
    idx = 0
    try:
        while idx < total_frames:
            # Agrupa hasta `group_frames`, respetando longitud real
            frames_left = total_frames - idx
            frames_this_round = min(group_frames, frames_left)

            start = idx * frame_len
            end = min(len(audio_raw), start + frames_this_round * frame_len)
            chunk = audio_raw[start:end]

            # Padding si último paquete incompleto
            if len(chunk) % frame_len:
                pad = frame_len - (len(chunk) % frame_len)
                chunk += b"\xFF" * pad  # 0xFF = silencio μ‑law

            # Control de pre‑buffer ------------------------------------------
            now          = time.perf_counter()
            elapsed_ms   = (now - start_t) * 1000
            audio_time_ms = idx * 20  # 20 ms por frame ya enviado
            ahead_ms      = audio_time_ms - elapsed_ms
            if ahead_ms > max_ahead_ms:
                sleep_needed = (ahead_ms - max_ahead_ms) / 1000
                if sleep_needed > 0:
                    await asyncio.sleep(sleep_needed)

            # Serializar + enviar
            payload64 = base64.b64encode(chunk).decode()
            try:
                await websocket_send(json.dumps({
                    "event": "media",
                    "streamSid": stream_sid,
                    "media": {"payload": payload64},
                }))
            except Exception as ws_exc:
                logger.warning("⚠️ websocket_send falló: %s", ws_exc)
                await _safe_send_mark(websocket_send, stream_sid, "error")
                return

            idx += frames_this_round

            # Pacing para siguiente grupo
            await asyncio.sleep(0)  # cede control al loop

    finally:
        envio_ms = (time.perf_counter() - ts_send_start) * 1000
        logger.info("📶 Audio enviado a Twilio en %.1f ms", envio_ms)

    # 5️⃣ Marca de fin
    await _safe_send_mark(websocket_send, stream_sid, "end_of_tts")
    logger.info("🏁 Audio completo enviado a Twilio.")


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------
async def _safe_send_mark(send: WebSocketSend, stream_sid: str, name: str) -> None:
    """Envía un evento *mark* salvaguardado con try/except."""
    try:
        await send(json.dumps({
            "event": "mark",
            "streamSid": stream_sid,
            "mark": {"name": name},
        }))
    except Exception as exc:
        logger.debug("(ignorado) No se pudo enviar mark '%s': %s", name, exc)
