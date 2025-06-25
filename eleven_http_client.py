"""
eleven_http_client.py
────────────────────────────────────────────────────────────────────────────
Obtiene audio TTS de ElevenLabs (μ-law 8 kHz) por HTTP → lo decodifica a
PCM lineal 16-bit 8 kHz → lo envía a Twilio Media Streams en frames de
320 bytes (20 ms) vía WebSocket.

⚠️  Requiere que tu TwiML incluya:
    <Parameter name="content-type" value="audio/raw"/>
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Awaitable, Callable

import audioop  # type: ignore
import httpx
from decouple import config

# ──────────────────────────────  Config  ──────────────────────────────────
logger = logging.getLogger("eleven_http_client")
logger.setLevel(logging.INFO)

ELEVEN_LABS_API_KEY: str = config("ELEVEN_LABS_API_KEY")
ELEVEN_LABS_VOICE_ID: str = config("ELEVEN_LABS_VOICE_ID")

STREAM_ENDPOINT = (
    f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_LABS_VOICE_ID}/stream"
)

HEADERS = {
    "xi-api-key": ELEVEN_LABS_API_KEY,
    "Content-Type": "application/json",
}

REQUEST_BODY = {
    "model_id": "eleven_multilingual_v2",
    "text": "",  # ← se rellena en runtime
    "voice_settings": {
        "stability": 0.7,
        "style": 0.5,
        "use_speaker_boost": False,
        "speed": 1.2,
    },
    # Plan básico ⇒ solo ulaw_8000 / alaw_8000 a 8 kHz
    "output_format": "ulaw_8000",
}

# 20 ms de audio PCM 16-bit / 8 kHz / mono = 160 muestras × 2 bytes
CHUNK_PCM = 320


# ─────────────────────  Función pública  ──────────────────────────────────
async def send_tts_http_to_twilio(
    *,
    text: str,
    stream_sid: str,
    websocket_send: Callable[[str], Awaitable[None]],
    chunk_size: int = CHUNK_PCM * 50,  # ≈ 1 s de audio
    volume_multiplier: float = 1.0,
) -> None:
    """
    Convierte *text* en TTS usando ElevenLabs y envía los bytes PCM a Twilio.

    Parameters
    ----------
    text : str
        Texto a sintetizar.
    stream_sid : str
        StreamSid entregado por Twilio (se incluye en cada JSON).
    websocket_send : Callable
        Función async `send_text()` o `send_json()` del WebSocket de Twilio.
    chunk_size : int, optional
        Tamaño de lotes antes de trocear en frames de 320 bytes.
    volume_multiplier : float, optional
        Ganancia a aplicar sobre PCM (1.0 = sin cambio).
    """
    if not text.strip():
        logger.warning("[EL-HTTP] Texto vacío recibido — se omite la llamada.")
        return

    logger.info("[EL-HTTP] Iniciando HTTP → ElevenLabs TTS …")

    body = REQUEST_BODY.copy()
    body["text"] = text.strip()

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            async with client.stream(
                "POST", STREAM_ENDPOINT, json=body, headers=HEADERS
            ) as response:
                if response.status_code != 200:
                    logger.error(
                        f"[EL-HTTP] ElevenLabs devolvió {response.status_code}: {response.text}"
                    )
                    return

                buffer = bytearray()

                async for g711_chunk in response.aiter_bytes():
                    if not g711_chunk:
                        continue

                    # 1️⃣ μ-law → PCM 16 bit
                    try:
                        pcm_piece = audioop.ulaw2lin(g711_chunk, 2)
                    except Exception as e:
                        logger.warning(f"[EL-HTTP] ulaw2lin falló: {e}")
                        continue

                    buffer.extend(pcm_piece)

                    while len(buffer) >= chunk_size:
                        send_chunk = bytes(buffer[:chunk_size])
                        del buffer[:chunk_size]
                        await _send_pcm_chunk(
                            send_chunk, stream_sid, websocket_send, volume_multiplier
                        )

                # Enviar sobrante
                if buffer:
                    await _send_pcm_chunk(
                        bytes(buffer), stream_sid, websocket_send, volume_multiplier
                    )

    except Exception as exc:  # noqa: BLE001
        logger.error(f"[EL-HTTP] Error general en HTTP Elabs: {exc}")


# ───────────────────────  Helpers internos  ───────────────────────────────
async def _send_pcm_chunk(
    pcm_bytes: bytes,
    stream_sid: str,
    websocket_send: Callable[[str], Awaitable[None]],
    volume_multiplier: float = 1.0,
) -> None:
    """
    Envía *pcm_bytes* a Twilio en frames de 320 bytes con Base64.

    Se aplica ganancia (sobre PCM) si `volume_multiplier` ≠ 1.0.
    """
    # Ajuste de volumen (solo sobre PCM; seguro)
    if volume_multiplier != 1.0:
        try:
            pcm_bytes = audioop.mul(pcm_bytes, 2, volume_multiplier)
        except Exception as e:
            logger.warning(f"[EL-HTTP] audioop.mul() falló: {e}")

    for i in range(0, len(pcm_bytes), CHUNK_PCM):
        frame = pcm_bytes[i : i + CHUNK_PCM]
        if not frame:
            continue

        payload_b64 = base64.b64encode(frame).decode("ascii")
        try:
            await websocket_send(
                json.dumps(
                    {
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": payload_b64},
                    }
                )
            )
        except Exception as e:
            logger.error(f"[EL-HTTP] No se pudo enviar frame a Twilio: {e}")
