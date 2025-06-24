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
    "model_id": "eleven_multilingual_v2",
    "text": "",
    "voice_settings": {
        "stability": 0.7,
        "style": 0.5,
        "use_speaker_boost": False,
        "speed": 1.2,
    },
    
    "output_format": "ulaw_8000",
}


# ---------------------------------------------------------------------------
# Función pública que llama el cliente 
# ---------------------------------------------------------------------------
async def send_tts_http_to_twilio(
    *,
    text: str,
    stream_sid: str,
    websocket_send: Callable[[str], Awaitable[None]],
    chunk_size: int = 160 * 50,          # ≈ 1 s de audio (20 ms * 50)
    volume_multiplier: float = 1.0,      # ↕️ sube / baja volumen antes de enviar
) -> None:
    """
    Genera TTS por HTTP y lo envía a Twilio en frames de 160 bytes μ-law.

    Args:
        text            Texto a convertir.
        stream_sid      Stream SID que Twilio espera.
        websocket_send  Función async que envía mensajes JSON al WS de Twilio.
        chunk_size      Tamaño de lote (múltiplo de 160 PCM) que convertimos cada vez.
        volume_multiplier  Escala la amplitud antes de convertir a μ-law.
    """

    if not text.strip():
        logger.warning("[EL-HTTP] Texto vacío recibido — omitido.")
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

                buffer   = bytearray()
                leftover = b""                     # ← byte impar pendiente

                async for raw_chunk in response.aiter_bytes():
                    if not raw_chunk:
                        continue

                    # ▸ Asegurar longitud par (PCM = 16 bit)
                    if leftover:
                        raw_chunk = leftover + raw_chunk
                        leftover = b""
                    if len(raw_chunk) % 2 == 1:
                        leftover = raw_chunk[-1:]
                        raw_chunk = raw_chunk[:-1]

                    buffer.extend(raw_chunk)

                    while len(buffer) >= chunk_size:
                        pcm_chunk = bytes(buffer[:chunk_size])
                        del buffer[:chunk_size]
                        await _convert_and_send_chunk(
                            pcm_chunk,
                            stream_sid,
                            websocket_send,
                            volume_multiplier,
                        )

                # Fin del stream — descartar byte impar si quedó
                if leftover:
                    logger.warning("[EL-HTTP] Stream terminó con 1 byte impar — descartado.")

                if buffer:
                    # Garantizar longitud par
                    if len(buffer) % 2 == 1:
                        buffer = buffer[:-1]
                    await _convert_and_send_chunk(
                        bytes(buffer),
                        stream_sid,
                        websocket_send,
                        volume_multiplier,
                    )

    except Exception as e:  # noqa: BLE001
        logger.error(f"[EL-HTTP] Error general en HTTP ELabs: {e}")



# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------
async def _convert_and_send_chunk(
    ulaw_bytes: bytes,
    stream_sid: str,
    websocket_send: Callable[[str], Awaitable[None]],
    volume_multiplier: float = 1.0,
) -> None:
    """
    Recibe audio µ-law 8 kHz y lo envía a Twilio en frames de 160 bytes (20 ms).

    • Si volume_multiplier ≠ 1.0, aplica ganancia con audioop.mul(width=1).
    • No realiza conversión de formato: Eleven Labs ya entrega µ-law.
    """
    # 1️⃣  Ajuste opcional de volumen
    try:
        if volume_multiplier != 1.0:
            ulaw_bytes = audioop.mul(ulaw_bytes, 1, volume_multiplier)  # width=1 → µ-law
    except Exception as e:
        logger.warning(f"[EL-HTTP] Error ajustando volumen: {e}")
        return

    # 2️⃣  Envía en frames de 160 bytes (20 ms)
    for i in range(0, len(ulaw_bytes), 160):
        frame = ulaw_bytes[i : i + 160]
        if not frame or len(frame) < 160:
            # Si el stream termina con un frame incompleto, lo ignoramos.
            if frame:
                logger.warning("[EL-HTTP] Stream terminó con frame incompleto — descartado.")
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
