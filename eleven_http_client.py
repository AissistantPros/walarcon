# eleven_http_client.py
# --------------------------------------------------
# Descarga audio μ-law 8kHz crudo desde Eleven Labs
# y lo envía a Twilio en chunks de 160 bytes.
# --------------------------------------------------

import base64
import time
import requests
import asyncio
import logging
import json
import audioop # type: ignore
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
            "stability": 0.75,
            "style": 0.45,
            "use_speaker_boost": True,
            "speed": 1.2
        }
    }

    try:
        ts_elabs_request = time.perf_counter()
        response = requests.post(url, json=payload, headers=headers, stream=True)
        response.raise_for_status()

        audio_buffer = BytesIO()
        for chunk in response.iter_content(chunk_size=4096):
            if chunk:
                if ts_elabs_first_chunk is None:
                    ts_elabs_first_chunk = time.perf_counter()
                    latency_ms = (ts_elabs_first_chunk - ts_elabs_request) * 1000
                    logger.info(f"⏱️ ElevenLabs respondió primer chunk tras {latency_ms:.1f} ms")
                audio_buffer.write(chunk)

        audio_data = audio_buffer.getvalue()


        # Amplifica el audio μ-law
        GAIN = 1  # Puedes probar 1.5 o 2.5 si quieres afinar más

        try:
            audio_data = audioop.mul(audio_data, 1, GAIN)
            logger.info(f"🔊 Audio amplificado con ganancia x{GAIN}")
        except Exception as e:
            logger.warning(f"❌ Error al amplificar audio: {e}")



        # ── Si comienza con RIFF es un WAV → quita los primeros 44 bytes ──
        if audio_data[:4] == b"RIFF":
            logger.warning("⚠️  ElevenLabs devolvió WAV; eliminando cabecera de 44 bytes")
            audio_data = audio_data[44:]



        logger.info(f"✅ Audio TTS recibido ({len(audio_data)} bytes)")

        # Enviar en chunks de 160 bytes (20 ms por frame a 8kHz)
        frame_size = 160
        total_frames = len(audio_data) // frame_size
        logger.info(f"📤 Enviando {total_frames} frames a Twilio…")


        ts_send_start = time.perf_counter()
        for i in range(0, len(audio_data), frame_size):
            frame = audio_data[i:i + frame_size]
            if len(frame) < frame_size:
                frame = frame.ljust(frame_size, b'\xff')  # padding con silencio μ-law
            await websocket_send(json.dumps({
                "event": "media",
                "streamSid": stream_sid,
                "media": {
                    "payload": base64.b64encode(frame).decode("ascii")

                }
            }))
            await asyncio.sleep(0.02)  # espera 20 ms por frame (match con realtime)
        ts_send_end = time.perf_counter()
        envio_ms = (ts_send_end - ts_send_start) * 1000
        logger.info(f"📶 Audio enviado a Twilio en {envio_ms:.1f} ms")
        
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
