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
    return audio_bytes

class TwilioWebSocketManager:
    """
    Maneja la conexión WebSocket con Twilio y envía audio a Deepgram.
    """

    def __init__(self):
        self.call_ended = False
        self.stream_sid = None
        self.stt_streamer = None

    async def handle_twilio_websocket(self, websocket: WebSocket):
        """
        Inicia la comunicación con Twilio y envía el audio recibido a Deepgram.
        """
        await websocket.accept()
        logger.info("📡 Conexión WebSocket aceptada con Twilio.")

        try:
            self.stt_streamer = DeepgramSTT(stt_callback)
            asyncio.create_task(self.stt_streamer.start_streaming())
        except Exception as e:
            logger.error(f"❌ Error inicializando Deepgram STT: {e}")
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
                    self.stt_streamer.send_audio(mulaw_chunk)

                elif event_type == "stop":
                    logger.info("🛑 Twilio envió evento 'stop'.")
                    await self._hangup_call(websocket)
                    break

        except Exception as e:
            logger.error(f"❌ Error en handle_twilio_websocket: {e}")
            await self._hangup_call(websocket)
        finally:
            if not self.call_ended:
                await self._hangup_call(websocket)
            logger.info("📴 WebSocket con Twilio cerrado.")
