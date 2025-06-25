import base64
import json
import logging
import audioop # type: ignore
import asyncio
from decouple import config
import httpx
from typing import Awaitable, Callable

logger = logging.getLogger("eleven_http_client")
logger.setLevel(logging.INFO)

# --- Configuración ---
ELEVEN_LABS_API_KEY = config("ELEVEN_LABS_API_KEY")
ELEVEN_LABS_VOICE_ID = config("ELEVEN_LABS_VOICE_ID")
STREAM_ENDPOINT = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_LABS_VOICE_ID}/stream"
HEADERS = {
    "xi-api-key": ELEVEN_LABS_API_KEY,
    "Content-Type": "application/json",
}

# --- Parámetros de la Petición ---
REQUEST_BODY = {
    "model_id": "eleven_turbo_v2",  # Modelo de baja latencia
    "text": "",
    "voice_settings": {
        "stability": 0.7,
        "style": 0.5,
        "use_speaker_boost": False,
        "speed": 1.2,
    },
    "output_format": "pcm_8000",  # PCM para mínimo delay
    "optimize_streaming_latency": 3,  # Máxima optimización de latencia
}

# Tamaños de frame (20ms de audio)
PCM_FRAME_SIZE = 320  # 8000 Hz * 2 bytes * 0.02s = 320 bytes (PCM 16-bit)
ULAW_FRAME_SIZE = 160  # 8000 Hz * 1 byte * 0.02s = 160 bytes (μ-law)

async def send_tts_to_twilio(
    text: str,
    stream_sid: str,
    websocket_send: Callable[[str], Awaitable[None]]
) -> None:
    """Envía TTS a Twilio con la menor latencia posible"""
    if not text.strip():
        logger.warning("Texto vacío - omitido")
        return

    body = {**REQUEST_BODY, "text": text.strip()}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Iniciamos medición de latencia
            start_time = asyncio.get_event_loop().time()
            first_chunk_received = False
            
            async with client.stream(
                "POST", 
                STREAM_ENDPOINT, 
                json=body, 
                headers=HEADERS
            ) as response:
                if response.status_code != 200:
                    error = await response.aread()
                    logger.error(f"Error ElevenLabs: {response.status_code} - {error}")
                    return

                # Buffer para datos PCM (16-bit little-endian)
                pcm_buffer = bytearray()
                
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    
                    # Registrar primer chunk recibido
                    if not first_chunk_received:
                        first_chunk_time = asyncio.get_event_loop().time()
                        latency = first_chunk_time - start_time
                        logger.info(f"Primer chunk recibido en {latency:.3f}s")
                        first_chunk_received = True
                    
                    pcm_buffer.extend(chunk)

                    # Procesar y enviar tan pronto como tengamos un frame completo
                    while len(pcm_buffer) >= PCM_FRAME_SIZE:
                        # Extraer un frame PCM
                        pcm_frame = bytes(pcm_buffer[:PCM_FRAME_SIZE])
                        del pcm_buffer[:PCM_FRAME_SIZE]
                        
                        # Convertir a μ-law (operación ultra rápida)
                        ulaw_frame = audioop.lin2ulaw(pcm_frame, 2)
                        
                        # Enviar inmediatamente a Twilio
                        media_message = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {
                                "payload": base64.b64encode(ulaw_frame).decode("ascii")
                            }
                        }
                        await websocket_send(json.dumps(media_message))
                
                # Procesar datos residuales
                if pcm_buffer:
                    # Asegurar tamaño par (cada muestra son 2 bytes)
                    if len(pcm_buffer) % 2 != 0:
                        pcm_buffer = pcm_buffer[:-1]
                    
                    if pcm_buffer:
                        ulaw_frame = audioop.lin2ulaw(bytes(pcm_buffer), 2)
                        
                        # Rellenar con silencio si es necesario
                        if len(ulaw_frame) < ULAW_FRAME_SIZE:
                            ulaw_frame += b'\xff' * (ULAW_FRAME_SIZE - len(ulaw_frame))
                        
                        await websocket_send(json.dumps({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {
                                "payload": base64.b64encode(ulaw_frame).decode("ascii")
                            }
                        }))

                # Mensaje de finalización
                if first_chunk_received:
                    total_time = asyncio.get_event_loop().time() - start_time
                    logger.info(f"Audio completo enviado en {total_time:.2f}s")

    except Exception as e:
        logger.error(f"Error en TTS: {str(e)}")
        # Enviar marca de finalización en caso de error
        await websocket_send(json.dumps({
            "event": "mark",
            "streamSid": stream_sid,
            "mark": {
                "name": "error"
            }
        }))