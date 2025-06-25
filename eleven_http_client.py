# eleven_http_client.py
# ---------------------------------------------------------------
# HTTP → ElevenLabs TTS  → WebSocket Twilio  (PCM 16-bit / 8 kHz)
# ---------------------------------------------------------------
import base64
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

import audioop  # type: ignore
import httpx
from decouple import config

# ── Logging ────────────────────────────────────────────────────
logger = logging.getLogger("eleven_http_client")
logger.setLevel(logging.INFO)

# ── Claves y constantes ────────────────────────────────────────
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
        "speed": 1.20,
    },
    "output_format": "ulaw_8000",  # μ-law 8 kHz (plan básico)
}

# ── Opcional: volcado de chunks brutos para depuración ─────────
DEBUG_TTS_DUMP = bool(int(os.getenv("DEBUG_TTS_DUMP", "0")))
if DEBUG_TTS_DUMP:
    DUMP_DIR = Path("tts_dumps")
    DUMP_DIR.mkdir(exist_ok=True)
    logger.info(f"[EL-HTTP] Dump de chunks ACTIVADO en: {DUMP_DIR.resolve()}")

# ---------------------------------------------------------------------------
# Función pública
# ---------------------------------------------------------------------------
async def send_tts_http_to_twilio(
    *,
    text: str,
    stream_sid: str,
    websocket_send: Callable[[str], Awaitable[None]],
    chunk_size: int = 160 * 50,        # 1 s de audio (20 ms × 50)
    volume_multiplier: float = 2.0,    # ganancia sobre PCM
) -> None:
    """
    Consume el endpoint streaming de ElevenLabs, convierte μ-law → PCM 16-bit,
    aplica ganancia opcional y envía frames de 320 bytes (20 ms) a Twilio.
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

                buffer = bytearray()

                async for raw_chunk in response.aiter_bytes():
                    if not raw_chunk:
                        continue

                    # ── Volcado opcional del chunk μ-law crudo ───────────────
                    if DEBUG_TTS_DUMP:
                        ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S-%f")
                        (DUMP_DIR / f"{ts}_raw.ulaw").write_bytes(raw_chunk)

                    # μ-law → PCM 16-bit (8 kHz)
                    pcm_chunk = audioop.ulaw2lin(raw_chunk, 2)

                    # Ganancia (solo sobre PCM)
                    if volume_multiplier != 1.0:
                        pcm_chunk = audioop.mul(pcm_chunk, 2, volume_multiplier)

                    buffer.extend(pcm_chunk)

                    # ── Procesar mientras haya al menos chunk_size bytes ─────
                    while len(buffer) >= chunk_size:
                        to_send = bytes(buffer[:chunk_size])
                        del buffer[:chunk_size]
                        await _send_pcm_to_twilio(
                            to_send, stream_sid, websocket_send
                        )

                # ── Restante al final ───────────────────────────────────────
                if buffer:
                    await _send_pcm_to_twilio(bytes(buffer), stream_sid, websocket_send)

    except Exception as e:  # noqa: BLE001
        logger.error(f"[EL-HTTP] Error general en HTTP ELabs: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------
async def _send_pcm_to_twilio(
    pcm_bytes: bytes,
    stream_sid: str,
    websocket_send: Callable[[str], Awaitable[None]],
) -> None:
    """
    Divide el PCM 16-bit / 8 kHz en frames de 320 bytes (20 ms) y los envía.
    """
    FRAME = 320  # 160 muestras × 2 bytes

    for i in range(0, len(pcm_bytes), FRAME):
        frame = pcm_bytes[i : i + FRAME]
        if len(frame) < FRAME:
            logger.warning("[EL-HTTP] Frame incompleto descartado.")
            continue

        payload_b64 = base64.b64encode(frame).decode("ascii")
        message = {
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": payload_b64},
        }
        try:
            await websocket_send(json.dumps(message))
        except Exception as e:
            logger.error(f"[EL-HTTP] No se pudo enviar frame a Twilio: {e}")
