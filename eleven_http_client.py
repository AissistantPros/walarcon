# eleven_http_client.py
# --------------------------------------------------
# Descarga audio μ-law 8kHz crudo desde Eleven Labs
# y lo envía a Twilio en chunks de 160 bytes.
# --------------------------------------------------

import requests
import asyncio
import logging
import json

from io import BytesIO

# ======= CREDENCIALES EN DURO (ajusta si lo quieres dinámico) =======
ELEVEN_LABS_API_KEY = "sk_35abd9f8c1e86371af7df3c4a877fde78d1108a74705c37b"
ELEVEN_LABS_VOICE_ID = "CaJslL1xziwefCeTNzHv"
# ====================================================================

logger = logging.getLogger("eleven_http_client")

async def send_tts_http_to_twilio(text, stream_sid, websocket_send):
    logger.info("🗣️ Solicitando TTS a Eleven Labs…")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_LABS_VOICE_ID}/stream?output_format=ulaw_8000"
    headers = {
        "xi-api-key": ELEVEN_LABS_API_KEY,
        "Accept": "audio/mulaw"  # ← fuerza μ-law crudo
    }
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.7,
            "style": 0.5,
            "use_speaker_boost": False,
            "speed": 1.0
        }
    }

    try:
        response = requests.post(url, json=payload, headers=headers, stream=True)
        response.raise_for_status()

        audio_buffer = BytesIO()
        for chunk in response.iter_content(chunk_size=4096):
            if chunk:
                audio_buffer.write(chunk)

        audio_data = audio_buffer.getvalue()
        logger.info(f"✅ Audio TTS recibido ({len(audio_data)} bytes)")

        # Enviar en chunks de 160 bytes (20 ms por frame a 8kHz)
        frame_size = 160
        total_frames = len(audio_data) // frame_size
        logger.info(f"📤 Enviando {total_frames} frames a Twilio…")

        for i in range(0, len(audio_data), frame_size):
            frame = audio_data[i:i + frame_size]
            if len(frame) < frame_size:
                frame = frame.ljust(frame_size, b'\xff')  # padding con silencio μ-law
            await websocket_send(json.dumps({
                "event": "media",
                "streamSid": stream_sid,
                "media": {
                    "payload": frame.hex()
                }
            }))
            await asyncio.sleep(0.02)  # espera 20 ms por frame (match con realtime)

        # Enviar marca de fin para saber que ya acabó el audio
        await websocket_send(json.dumps({
            "event": "mark",
            "streamSid": stream_sid,
            "mark": {"name": "end_of_tts"}
        }))
        logger.info("🏁 Audio completo enviado a Twilio.")

    except Exception as e:
        logger.error(f"🚨 Error al generar o enviar TTS: {str(e)}")
        await websocket_send(json.dumps({
            "event": "mark",
            "streamSid": stream_sid,
            "mark": {"name": "error"}
        }))
