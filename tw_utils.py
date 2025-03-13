# tw_utils.py - Manejo de WebSockets de Twilio con Deepgram

import json
import base64
import time
import asyncio
import logging
import os
from fastapi import WebSocket
from starlette.websockets import WebSocketState
from deepgram_stt import DeepgramSTT
from aiagent import generate_openai_response
from tts_utils import text_to_speech  # Devuelve audio en formato mu-law (bytes)

logging.getLogger("tts_utils").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

AUDIO_DIR = "audio"  # Carpeta para archivos pregrabados (aunque ya no usamos saludo.wav)

def stt_callback(transcript, is_final):
    """
    Procesa la transcripción de Deepgram y la envía a la IA si es final.
    """
    if is_final:
        asyncio.create_task(process_gpt_response(transcript))

async def process_gpt_response(user_text):
    """
    Llama a la IA con el historial de conversación y envía la respuesta TTS.
    """
    logger.info(f"🎙️ USUARIO: {user_text}")
    
    gpt_response = generate_openai_response([{"role": "user", "content": user_text}])
    logger.info(f"🤖 GPT: {gpt_response}")

    audio_bytes = text_to_speech(gpt_response)
    if not audio_bytes:
        logger.error("❌ No se pudo generar audio TTS.")
        return

    return audio_bytes

class TwilioWebSocketManager:
    """
    Maneja la conexión WebSocket con Twilio, usando Deepgram en vez de Google STT.
    """

    def __init__(self):
        self.call_ended = False
        self.stream_sid = None
        self.stt_streamer = None
        self.stream_start_time = time.time()

    async def handle_twilio_websocket(self, websocket: WebSocket):
        """
        Inicia la comunicación con Twilio y gestiona la transcripción de voz en tiempo real.
        """
        await websocket.accept()
        logger.info("📡 Conexión WebSocket aceptada con Twilio.")

        # 🟢 Generar el saludo dinámicamente con ElevenLabs
        saludo_texto = "Hola!!. Gracias por llamar al consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudar el día de hoy?"
        saludo_audio = text_to_speech(saludo_texto)

        if saludo_audio:
            await self._play_audio_bytes(websocket, saludo_audio)
            logger.info("✅ Saludo dinámico enviado.")

        try:
            self.stt_streamer = DeepgramSTT(stt_callback)
            asyncio.create_task(self.stt_streamer.start_streaming())
        except Exception as e:
            logger.error(f"❌ Error inicializando Deepgram STT: {e}", exc_info=True)
            await websocket.close(code=1011)
            return

        try:
            while True:
                message = await websocket.receive_text()
                data = json.loads(message)
                event_type = data.get("event")

                if event_type == "start":
                    self.stream_sid = data.get("streamSid")
                    logger.info(f"📞 Nuevo stream SID: {self.stream_sid}")

                elif event_type == "media":
                    payload_base64 = data["media"]["payload"]
                    mulaw_chunk = base64.b64decode(payload_base64)
                    await self.stt_streamer.start_streaming()

                elif event_type == "stop":
                    logger.info("🛑 Twilio envió evento 'stop'.")
                    await self._hangup_call(websocket)
                    break

        except Exception as e:
            logger.error(f"❌ Error en handle_twilio_websocket: {e}", exc_info=True)
            await self._hangup_call(websocket)
        finally:
            if not self.call_ended:
                await self._hangup_call(websocket)
            logger.info("📴 WebSocket con Twilio cerrado.")

    async def _play_audio_bytes(self, websocket: WebSocket, audio_bytes: bytes):
        """
        Envía audio generado en tiempo real (mu-law) a Twilio para su reproducción.
        """
        if not self.stream_sid or self.call_ended:
            return
        if websocket.client_state != WebSocketState.CONNECTED:
            return
        try:
            encoded = base64.b64encode(audio_bytes).decode("utf-8")
            await websocket.send_text(json.dumps({
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {
                    "payload": encoded
                }
            }))
            logger.info("🔊 Reproduciendo audio TTS dinámico")
        except Exception as e:
            logger.error(f"❌ Error reproduciendo audio TTS: {e}", exc_info=True)

    async def _hangup_call(self, websocket: WebSocket):
        """
        Termina la llamada y cierra todo.
        """
        if self.call_ended:
            return
        self.call_ended = True
        logger.info("📴 Terminando la llamada.")

        if self.stt_streamer:
            self.stt_streamer.close()

        try:
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close(code=1000)
                logger.info("✅ WebSocket cerrado correctamente.")
        except Exception as e:
            logger.error(f"❌ Error al cerrar WebSocket: {e}", exc_info=True)
