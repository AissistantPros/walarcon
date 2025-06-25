# eleven_http_client.py

# -------------------------------------------------------------------------
# HTTP → ElevenLabs (PCM 16-bit 8kHz) → [Amplify+Convert] → WebSocket Twilio (μ-law 8 kHz)
# -------------------------------------------------------------------------
import base64
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

import httpx
from decouple import config
import numpy as np  # <-- AÑADIDO para amplificación
import audioop      # type: ignore # <-- AÑADIDO para conversión a mu-law

logger = logging.getLogger("eleven_http_client")
logger.setLevel(logging.INFO)

# --- Credenciales ---
ELEVEN_LABS_API_KEY = config("ELEVEN_LABS_API_KEY")
ELEVEN_LABS_VOICE_ID = config("ELEVEN_LABS_VOICE_ID")

STREAM_ENDPOINT = (
    f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_LABS_VOICE_ID}/stream"
)
HEADERS = {"xi-api-key": ELEVEN_LABS_API_KEY, "Content-Type": "application/json"}

# --- Parámetros de la Petición a ElevenLabs ---
REQUEST_BODY = {
    "model_id": "eleven_multilingual_v2",
    "text": "",
    "voice_settings": {
        "stability": 0.7,
        "style": 0.5,
        "use_speaker_boost": False, # Lo dejamos en False ya que amplificaremos manualmente
        "speed": 1.20,
    },
    # ==================================================================
    # CAMBIO #1: Pedimos audio PCM lineal de 16-bit a 8000 Hz.
    # Esto nos da el audio "crudo" para poder procesarlo antes de enviarlo.
    # ==================================================================
    "output_format": "pcm_8000",
}

# --- Dump opcional para depuración ---
DEBUG_TTS_DUMP = bool(int(os.getenv("DEBUG_TTS_DUMP", "0")))
if DEBUG_TTS_DUMP:
    DUMP_DIR = Path("tts_dumps"); DUMP_DIR.mkdir(exist_ok=True)
    logger.info(f"[EL-HTTP] Dump ACTIVADO en: {DUMP_DIR.resolve()}")

# ==================================================================
# CAMBIO #2: Nueva función para procesar el audio PCM.
# Esta función toma el audio PCM, lo amplifica y lo convierte a mu-law.
# ==================================================================
def _amplify_and_convert_to_ulaw(pcm_chunk: bytes) -> bytes:
    """
    Amplifica un chunk de audio PCM de 16-bit y lo convierte a μ-law de 8-bit.
    """
    if not pcm_chunk:
        return b""

    # Asegurarse de que el chunk tenga un número par de bytes (cada muestra son 2 bytes)
    if len(pcm_chunk) % 2 != 0:
        logger.warning(f"[PCM-PROCESS] Chunk con número impar de bytes ({len(pcm_chunk)}), se descarta el último byte.")
        pcm_chunk = pcm_chunk[:-1]

    try:
        # --- Lógica de amplificación (tomada de tu código funcional anterior) ---
        volume_factor = 1.5 # Puedes ajustar este factor si es necesario

        # 1. Convertir bytes a un array de numpy (int16)
        pcm_array = np.frombuffer(pcm_chunk, dtype=np.int16)

        # 2. Amplificar (convirtiendo a float para la operación)
        amplified_array = pcm_array.astype(np.float32) * volume_factor

        # 3. Limitar los picos para evitar distorsión (clipping)
        clipped_array = np.clip(amplified_array, -32768, 32767)

        # 4. Convertir de nuevo a int16 y luego a bytes
        final_pcm_bytes = clipped_array.astype(np.int16).tobytes()

        # --- Conversión a μ-law ---
        ulaw_data = audioop.lin2ulaw(final_pcm_bytes, 2) # El 2 indica que el ancho de la muestra es de 2 bytes (16-bit)

        return ulaw_data

    except Exception as e:
        logger.error(f"[PCM-PROCESS] Error procesando chunk de audio: {e}", exc_info=True)
        return b""

# ---------------------------------------------------------------------------
async def send_tts_http_to_twilio(
    *,
    text: str,
    stream_sid: str,
    websocket_send: Callable[[str], Awaitable[None]],
    # Ajustamos el chunk_size para PCM (2 bytes por muestra)
    # 8000 bytes = 4000 muestras = 0.5 segundos de audio PCM 8kHz/16bit
    chunk_size: int = 8000,
) -> None:
    """
    Pide audio PCM a ElevenLabs, lo procesa (amplifica y convierte a μ-law),
    y luego lo envía a Twilio.
    """
    if not text.strip():
        logger.warning("[EL-HTTP] Texto vacío — omitido."); return

    logger.info("[EL-HTTP] Streaming TTS (PCM) desde ElevenLabs para procesar...")
    body = REQUEST_BODY.copy(); body["text"] = text.strip()

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            async with client.stream("POST", STREAM_ENDPOINT,
                                      json=body, headers=HEADERS) as resp:
                if resp.status_code != 200:
                    error_text = await resp.aread()
                    logger.error(f"[EL-HTTP] {resp.status_code}: {error_text!r}"); return

                buffer = bytearray()
                async for pcm_chunk in resp.aiter_bytes():
                    if not pcm_chunk: continue

                    buffer.extend(pcm_chunk)

                    # Procesamos y enviamos bloques de chunk_size
                    while len(buffer) >= chunk_size:
                        to_process = bytes(buffer[:chunk_size])
                        del buffer[:chunk_size]

                        # ==================================================================
                        # CAMBIO #3: Llamamos a nuestra nueva función de procesamiento.
                        # ==================================================================
                        ulaw_to_send = _amplify_and_convert_to_ulaw(to_process)

                        if ulaw_to_send:
                            await _send_ulaw_to_twilio(ulaw_to_send, stream_sid, websocket_send)

                # Procesar el resto final del buffer
                if buffer:
                    ulaw_to_send = _amplify_and_convert_to_ulaw(bytes(buffer))
                    if ulaw_to_send:
                        await _send_ulaw_to_twilio(ulaw_to_send, stream_sid, websocket_send)

    except Exception as e:
        logger.error(f"[EL-HTTP] Error: {e}", exc_info=True)

# ---------------------------------------------------------------------------
# Esta función no necesita cambios, ya que recibe el audio ya convertido a μ-law.
async def _send_ulaw_to_twilio(
    ulaw_bytes: bytes,
    stream_sid: str,
    websocket_send: Callable[[str], Awaitable[None]],
) -> None:
    """
    Envía frames de 160 bytes de audio μ-law a Twilio.
    """
    FRAME = 160  # 20 ms μ-law

    for i in range(0, len(ulaw_bytes), FRAME):
        frame = ulaw_bytes[i:i + FRAME]
        if len(frame) < FRAME:
            # Rellenar con silencio si el frame final no es múltiplo de 160
            frame += b'\xff' * (FRAME - len(frame))

        msg = {
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": base64.b64encode(frame).decode("ascii")},
        }
        try:
            await websocket_send(json.dumps(msg))
        except Exception as e:
            logger.error(f"[EL-HTTP] No se pudo enviar frame a Twilio: {e}")