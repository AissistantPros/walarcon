# eleven_http_client.py
# ---------------------------------------------------------------
# HTTP → ElevenLabs (μ-law 8 kHz) → WebSocket Twilio (μ-law 8 kHz)
# ---------------------------------------------------------------
import base64, json, logging, os
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

import httpx
from decouple import config

logger = logging.getLogger("eleven_http_client")
logger.setLevel(logging.INFO)

# ── Credenciales ───────────────────────────────────────────────
ELEVEN_LABS_API_KEY = config("ELEVEN_LABS_API_KEY")
ELEVEN_LABS_VOICE_ID = config("ELEVEN_LABS_VOICE_ID")

STREAM_ENDPOINT = (
    f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_LABS_VOICE_ID}/stream"
)
HEADERS = {"xi-api-key": ELEVEN_LABS_API_KEY, "Content-Type": "application/json"}

REQUEST_BODY = {
    "model_id": "eleven_multilingual_v2",
    "text": "",
    "voice_settings": {
        "stability": 0.7,
        "style": 0.5,
        "use_speaker_boost": False,
        "speed": 1.20,
    },
    "output_format": "ulaw_8000",          # ← μ-law 8 kHz
}

# ── Dump opcional de chunks para depurar ───────────────────────
DEBUG_TTS_DUMP = bool(int(os.getenv("DEBUG_TTS_DUMP", "0")))
if DEBUG_TTS_DUMP:
    DUMP_DIR = Path("tts_dumps"); DUMP_DIR.mkdir(exist_ok=True)
    logger.info(f"[EL-HTTP] Dump ACTIVADO en: {DUMP_DIR.resolve()}")

# ---------------------------------------------------------------------------
async def send_tts_http_to_twilio(
    *,
    text: str,
    stream_sid: str,
    websocket_send: Callable[[str], Awaitable[None]],
    chunk_size: int = 160 * 50,      # ≈1 s (50×20 ms)
) -> None:
    """
    Pide audio μ-law a ElevenLabs y lo re-envía a Twilio tal cual
    (frames de 160 bytes = 20 ms, conforme al content-type μ-law
    declarado en tu TwiML).
    """
    if not text.strip():
        logger.warning("[EL-HTTP] Texto vacío — omitido."); return

    logger.info("[EL-HTTP] Streaming TTS desde ElevenLabs…")
    body = REQUEST_BODY.copy(); body["text"] = text.strip()

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            async with client.stream("POST", STREAM_ENDPOINT,
                                      json=body, headers=HEADERS) as resp:
                if resp.status_code != 200:
                    logger.error(f"[EL-HTTP] {resp.status_code}: {resp.text}"); return

                buffer = bytearray()
                async for ulaw_chunk in resp.aiter_bytes():
                    if not ulaw_chunk: continue

                    # (Opcional) guarda el chunk crudo para pruebas
                    if DEBUG_TTS_DUMP:
                        ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S-%f")
                        (DUMP_DIR / f"{ts}.ulaw").write_bytes(ulaw_chunk)

                    buffer.extend(ulaw_chunk)

                    # Envía bloques de chunk_size (1 s) para no saturar
                    while len(buffer) >= chunk_size:
                        to_send = bytes(buffer[:chunk_size])
                        del buffer[:chunk_size]
                        await _send_ulaw_to_twilio(to_send, stream_sid, websocket_send)

                if buffer:  # resto final
                    await _send_ulaw_to_twilio(bytes(buffer), stream_sid, websocket_send)

    except Exception as e:
        logger.error(f"[EL-HTTP] Error: {e}", exc_info=True)

# ---------------------------------------------------------------------------
async def _send_ulaw_to_twilio(
    ulaw_bytes: bytes,
    stream_sid: str,
    websocket_send: Callable[[str], Awaitable[None]],
) -> None:
    FRAME = 160  # 20 ms μ-law

    for i in range(0, len(ulaw_bytes), FRAME):
        frame = ulaw_bytes[i:i + FRAME]
        if len(frame) < FRAME:
            logger.warning("[EL-HTTP] Frame incompleto descartado."); continue

        msg = {
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": base64.b64encode(frame).decode("ascii")},
        }
        try:
            await websocket_send(json.dumps(msg))
        except Exception as e:
            logger.error(f"[EL-HTTP] No se pudo enviar frame a Twilio: {e}")
