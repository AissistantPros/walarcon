# eleven_http_client.py
# ---------------------------------------------------------------
# HTTP → ElevenLabs TTS  → WebSocket Twilio  (μ-law 8 kHz / 20 ms)
# ---------------------------------------------------------------
import base64, json, logging, os
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable
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
        "speed": 1.20,
    },
    "output_format": "ulaw_8000",   # ← μ-law 8 kHz mono
}

# ── opcional: guardar chunks crudos para depurar ───────────────
DEBUG_TTS_DUMP = bool(int(os.getenv("DEBUG_TTS_DUMP", "0")))
if DEBUG_TTS_DUMP:
    DUMP_DIR = Path("tts_dumps")
    DUMP_DIR.mkdir(exist_ok=True)
    logger.info(f"[EL-HTTP] Dump de chunks activado en {DUMP_DIR.resolve()}")

# ───────────────────────────────────────────────────────────────
async def send_tts_http_to_twilio(
    *,
    text: str,
    stream_sid: str,
    websocket_send: Callable[[str], Awaitable[None]],
    frame_ms: int = 20,                # tamaño de frame en milisegundos
) -> None:
    """
    Pide TTS a ElevenLabs (μ-law 8 kHz) y reenvía los bytes tal cual
    a Twilio en marcos de 160 bytes (20 ms).
    """
    if not text.strip():
        logger.warning("[EL-HTTP] Texto vacío: no se envía TTS.")
        return

    logger.info("[EL-HTTP] Solicitando TTS a ElevenLabs…")
    body = REQUEST_BODY.copy()
    body["text"] = text.strip()

    FRAME = int(8000 * frame_ms / 1000)  # 160 muestras → 160 bytes μ-law

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            async with client.stream(
                "POST", STREAM_ENDPOINT, json=body, headers=HEADERS
            ) as resp:

                if resp.status_code != 200:
                    logger.error(f"[EL-HTTP] Respuesta {resp.status_code}: {resp.text}")
                    return

                remainder = bytearray()

                async for chunk in resp.aiter_bytes():
                    if not chunk:
                        continue

                    if DEBUG_TTS_DUMP:
                        ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S-%f")
                        (DUMP_DIR / f"{ts}.ulaw").write_bytes(chunk)

                    remainder.extend(chunk)

                    while len(remainder) >= FRAME:
                        frame = bytes(remainder[:FRAME])
                        del remainder[:FRAME]
                        await _send_ulaw_frame(frame, stream_sid, websocket_send)

                # último trozo (si quedó algo menor a 160 bytes, Twilio lo ignora)
                if remainder:
                    logger.warning("[EL-HTTP] Frame incompleto descartado al final.")

    except Exception as e:
        logger.error(f"[EL-HTTP] Error general: {e}", exc_info=True)

# ───────────────────────────────────────────────────────────────
async def _send_ulaw_frame(
    frame: bytes,
    stream_sid: str,
    websocket_send: Callable[[str], Awaitable[None]],
) -> None:
    try:
        await websocket_send(
            json.dumps(
                {
                    "event": "media",
                    "streamSid": stream_sid,
                    "media": {
                        "payload": base64.b64encode(frame).decode("ascii")
                    },
                }
            )
        )
    except Exception as e:
        logger.error(f"[EL-HTTP] No se pudo enviar frame a Twilio: {e}")
