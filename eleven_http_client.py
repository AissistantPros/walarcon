# eleven_http_client.py

import base64
import json
import logging
from typing import Callable, Awaitable
import audioop # type: ignore
import httpx
from decouple import config

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
    "model_id": "eleven_multilingual_v2",  # Modelo estable (puedes cambiarlo)
    "text": "",  # Se llena en runtime
    "voice_settings": {
        "stability": 0.7,
        "style": 0.5,
        "use_speaker_boost": False,
        "speed": 1.15,
    },
    "output_format": "pcm_8000",  # PCM 16‑bit, 8 kHz — fácil de convertir a μ‑law
}


# ---------------------------------------------------------------------------
# Función pública que llama el cliente 
# ---------------------------------------------------------------------------
async def send_tts_http_to_twilio(
    *,
    text: str,
    stream_sid: str,
    websocket_send: Callable[[str], Awaitable[None]],
    chunk_size: int = 160 * 50,
    volume_multiplier: float = 2.0,  # ← nuevo parámetro
) -> None:
    """Genera TTS por HTTP y lo envía chunk a chunk hacia Twilio.

    Args:
        text: Texto a convertir.
        stream_sid: Stream SID que Twilio espera.
        websocket_send: Función async que envía mensajes JSON al WS de Twilio.
        chunk_size: Número de bytes PCM por chunk (múltiplo de 160).
    """

    if not text.strip():
        logger.warning("[EL-HTTP] Texto vacío recibido  — omitido.")
        return

    logger.info("[EL-HTTP] Iniciando HTTP → ElevenLabs TTS …")

    body = REQUEST_BODY.copy()
    body["text"] = text.strip()

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            async with client.stream("POST", STREAM_ENDPOINT, json=body, headers=HEADERS) as response:
                if response.status_code != 200:
                    logger.error(
                        f"[EL-HTTP] ElevenLabs devolvió {response.status_code}: {response.text}"
                    )
                    return

                buffer = bytearray()
                async for raw_chunk in response.aiter_bytes():
                    if not raw_chunk:
                        continue
                    buffer.extend(raw_chunk)

                    # Procesamos en bloques definidos para no saturar Twilio
                    while len(buffer) >= chunk_size:
                        pcm_chunk = bytes(buffer[:chunk_size])
                        del buffer[:chunk_size]
                        await _convert_and_send_chunk(pcm_chunk, stream_sid, websocket_send, volume_multiplier)


                # Envía lo que quede al final
                if buffer:
                    await _convert_and_send_chunk(bytes(buffer), stream_sid, websocket_send, volume_multiplier)


    except Exception as e:  # noqa: BLE001
        logger.error(f"[EL-HTTP] Error general en HTTP ELabs: {e}")


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------
async def _convert_and_send_chunk(
    pcm_bytes: bytes,
    stream_sid: str,
    websocket_send: Callable[[str], Awaitable[None]],
    volume_multiplier: float = 1.0,  # ← nuevo parámetro
) -> None:
    """Convierte PCM 16‑bit a μ‑law 8 kHz y lo envía a Twilio en bloques de 160 bytes (20 ms)."""
    try:
        if volume_multiplier != 1.0:
            pcm_bytes = audioop.mul(pcm_bytes, 2, volume_multiplier)

        mulaw_bytes = audioop.lin2ulaw(pcm_bytes, 2)

    except Exception as e:
        logger.warning(f"[EL-HTTP] Error convirtiendo a μ-law: {e}")
        return

    # 🔄 Divide en frames de 160 bytes (≈20 ms cada uno)
    for i in range(0, len(mulaw_bytes), 160):
        frame = mulaw_bytes[i:i + 160]
        if not frame:
            continue

        payload_b64 = base64.b64encode(frame).decode("utf-8")
        message = {
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": payload_b64},
        }
        try:
            await websocket_send(json.dumps(message))
        except Exception as e:
            logger.error(f"[EL-HTTP] No se pudo enviar frame a Twilio: {e}")
