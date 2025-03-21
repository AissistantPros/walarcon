import json
import base64
import time
import asyncio
import logging
import os
from fastapi import WebSocket
from starlette.websockets import WebSocketState
from deepgram_stt_streamer import DeepgramSTTStreamer
from aiagent import generate_openai_response
from tts_utils import text_to_speech
from utils import get_cancun_time

# Configurar logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

AUDIO_DIR = "audio"

def stt_callback_factory(manager):
    def stt_callback(transcript, is_final):
        if transcript:
            current_time = time.time()
            if current_time - manager.last_final_time < 2.0:
                return
            manager.current_partial = transcript
            manager.last_partial_time = current_time
            if is_final:
                logger.info(f"🎙️ USUARIO (final): {transcript}")
                manager.last_final_time = time.time()
                asyncio.create_task(manager.process_gpt_response(transcript, manager.websocket))
    return stt_callback

def get_greeting_by_time():
    now = get_cancun_time()
    hour = now.hour
    minute = now.minute
    if 3 <= hour < 12:
        return "Buenos días, soy Dany, la asistente virtual del Dr. Alarcón. ¿En qué puedo ayudarle?"
    elif (hour == 19 and minute >= 30) or hour >= 20 or hour < 3:
        return "Buenas noches, soy Dany, la asistente virtual del Dr. Alarcón. ¿En qué puedo ayudarle?"
    else:
        return "Buenas tardes, soy Dany, la asistente virtual del Dr. Alarcón. ¿En qué puedo ayudarle?"

class TwilioWebSocketManager:
    def __init__(self):
        self.call_ended = False
        self.stream_sid = None
        self.stt_streamer = None
        self.stream_start_time = time.time()
        self.current_partial = ""
        self.last_partial_time = 0.0
        self.last_final_time = 0.0
        self.silence_threshold = 1.5
        self._silence_task = None
        self.websocket = None

    async def handle_twilio_websocket(self, websocket: WebSocket):
        self.websocket = websocket
        await websocket.accept()
        logger.info("📞 Llamada iniciada. WebSocket aceptado.")

        try:
            self.stt_streamer = DeepgramSTTStreamer(stt_callback_factory(self))
            await self.stt_streamer.start_streaming()
            logger.info("✅ Deepgram STT iniciado correctamente.")
        except Exception as e:
            logger.error(f"❌ Error iniciando Deepgram STT: {e}", exc_info=True)
            await websocket.close(code=1011)
            return

       # self._silence_task = asyncio.create_task(self._silence_watcher(websocket))
        self.stream_start_time = time.time()

        try:
            while True:
                message = await websocket.receive_text()
                data = json.loads(message)
                event_type = data.get("event")

                if event_type == "start":
                    self.stream_sid = data.get("streamSid")
                    logger.info(f"🔗 stream SID: {self.stream_sid}")
                    saludo = get_greeting_by_time()
                    logger.info(f"🤖 Saludo inicial: {saludo}")
                    saludo_audio = text_to_speech(saludo)
                    await self._play_audio_bytes(websocket, saludo_audio)

                elif event_type == "media":
                    payload_base64 = data["media"].get("payload")
                    if payload_base64:
                        mulaw_chunk = base64.b64decode(payload_base64)
                        await self.stt_streamer.send_audio(mulaw_chunk)

                elif event_type == "stop":
                    logger.info("🛑 Evento 'stop' recibido desde Twilio.")
                    break

        except Exception as e:
            logger.error(f"❌ Error en WebSocket: {e}", exc_info=True)
        finally:
            await self._shutdown()

    async def process_gpt_response(self, user_text: str, websocket: WebSocket):
        try:
            if self.call_ended or websocket.client_state != WebSocketState.CONNECTED:
                return
            gpt_response = generate_openai_response([{"role": "user", "content": user_text}])
            logger.info(f"🤖 IA: {gpt_response}")
            audio_bytes = text_to_speech(gpt_response)
            await self._play_audio_bytes(websocket, audio_bytes)
        except Exception as e:
            logger.error(f"❌ Error procesando respuesta de IA: {e}", exc_info=True)

    #async def _silence_watcher(self, websocket: WebSocket):
        #while not self.call_ended and websocket.client_state == WebSocketState.CONNECTED:
         #   await asyncio.sleep(0.2)
          #  if self.current_partial and (time.time() - self.last_partial_time > self.silence_threshold):
           #     final_text = self.current_partial.strip()
            #    self.current_partial = ""
             #   asyncio.create_task(self.process_gpt_response(final_text, websocket))

    async def _play_audio_bytes(self, websocket: WebSocket, audio_bytes: bytes):
        if not self.stream_sid or self.call_ended or websocket.client_state != WebSocketState.CONNECTED:
            return
        try:
            encoded = base64.b64encode(audio_bytes).decode("utf-8")
            await websocket.send_text(json.dumps({
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": encoded}
            }))
            logger.info("🔊 Audio TTS enviado a Twilio.")
        except Exception as e:
            logger.error(f"❌ Error enviando audio TTS: {e}", exc_info=True)

    async def _shutdown(self):
        logger.info("📴 Cerrando conexión y limpiando recursos...")
        self.call_ended = True
        if self._silence_task and not self._silence_task.done():
            self._silence_task.cancel()
        if self.stt_streamer:
            await self.stt_streamer.close()
        if self.websocket and self.websocket.client_state == WebSocketState.CONNECTED:
            await self.websocket.close()
        logger.info("✅ Cierre completo del WebSocket Manager.")
