# eleven_http_client.py

# -------------------------------------------------------------------------
# HTTP → ElevenLabs (PCM 16-bit 8kHz) → [Amplify+Convert] → WebSocket Twilio (μ-law 8 kHz)
# -------------------------------------------------------------------------
import base64
import json
import logging
import os
import httpx
import audioop # type: ignore
from decouple import config
from typing import Awaitable, Callable

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
        "use_speaker_boost": False,
        "speed": 1.20,
    },
    "output_format": "pcm_8000",
}

def _process_audio(pcm_data: bytes) -> bytes:
    """Procesa audio PCM usando el método antiguo que funcionaba"""
    if not pcm_data:
        return b""
    
    try:
        # 1. Amplificación con audioop (más confiable que NumPy)
        # Factor de amplificación 2.5 como en el código antiguo
        amplified = audioop.mul(pcm_data, 2, 2.5)
        
        # 2. Conversión a μ-law
        ulaw_data = audioop.lin2ulaw(amplified, 2)
        return ulaw_data
    except Exception as e:
        logger.error(f"Error procesando audio: {e}")
        return b""

async def send_tts_http_to_twilio(
    *,
    text: str,
    stream_sid: str,
    websocket_send: Callable[[str], Awaitable[None]],
) -> None:
    if not text.strip():
        logger.warning("Texto vacío - omitido")
        return

    logger.info("Streaming TTS desde ElevenLabs...")
    body = REQUEST_BODY.copy()
    body["text"] = text.strip()

    # Buffer para acumular PCM (asegura muestras completas)
    pcm_buffer = bytearray()
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream("POST", STREAM_ENDPOINT, json=body, headers=HEADERS) as resp:
                if resp.status_code != 200:
                    error_text = await resp.aread()
                    logger.error(f"Error ElevenLabs: {resp.status_code}: {error_text}")
                    return
                
                async for chunk in resp.aiter_bytes():
                    if not chunk:
                        continue
                    
                    # Acumular en buffer
                    pcm_buffer.extend(chunk)
                    
                    # Procesar solo cuando tengamos suficiente datos (múltiplo de 2 para muestras de 16 bits)
                    # Vamos a procesar en bloques de 1600 bytes (100ms de audio: 8000 muestras/seg * 2 bytes/muestra * 0.1 seg = 1600 bytes)
                    while len(pcm_buffer) >= 1600:
                        to_process = bytes(pcm_buffer[:1600])
                        del pcm_buffer[:1600]
                        
                        # Procesar y convertir
                        ulaw_audio = _process_audio(to_process)
                        
                        if ulaw_audio:
                            await _send_ulaw_to_twilio(ulaw_audio, stream_sid, websocket_send)
                
                # Procesar cualquier dato restante
                if pcm_buffer:
                    # Asegurar tamaño par (cada muestra son 2 bytes)
                    if len(pcm_buffer) % 2 != 0:
                        # Si es impar, descartamos el último byte
                        pcm_buffer = pcm_buffer[:-1]
                    
                    if pcm_buffer:
                        ulaw_audio = _process_audio(bytes(pcm_buffer))
                        if ulaw_audio:
                            await _send_ulaw_to_twilio(ulaw_audio, stream_sid, websocket_send)
    
    except Exception as e:
        logger.error(f"Error en TTS HTTP: {e}")

async def _send_ulaw_to_twilio(
    ulaw_bytes: bytes,
    stream_sid: str,
    websocket_send: Callable[[str], Awaitable[None]],
) -> None:
    FRAME_SIZE = 160  # 20ms de audio μ-law (20ms * 8000 muestras/seg * 1 byte/muestra = 160 bytes)
    
    # Enviamos el audio en frames de 160 bytes (20ms)
    for i in range(0, len(ulaw_bytes), FRAME_SIZE):
        frame = ulaw_bytes[i:i + FRAME_SIZE]
        # Si el frame final es más pequeño, lo rellenamos con silencio (0xff para μ-law)
        if len(frame) < FRAME_SIZE:
            frame += b'\xff' * (FRAME_SIZE - len(frame))
        
        msg = {
            "event": "media",
            "streamSid": stream_sid,
            "media": {
                "payload": base64.b64encode(frame).decode("ascii")
            },
        }
        try:
            await websocket_send(json.dumps(msg))
        except Exception as e:
            logger.error(f"Error enviando frame a Twilio: {e}")