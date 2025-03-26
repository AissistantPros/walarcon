#tw_utils.py

import json
import base64
import time
import asyncio
import logging
import os
from fastapi import WebSocket
from starlette.websockets import WebSocketState
from consultarinfo import load_consultorio_data_to_cache
from deepgram_stt_streamer import DeepgramSTTStreamer
from aiagent import generate_openai_response
from tts_utils import text_to_speech
from utils import get_cancun_time
import re

# 🔸 Importamos las funciones y variables para manipular el cache
from buscarslot import load_free_slots_to_cache, free_slots_cache, last_cache_update

CURRENT_CALL_MANAGER = None  # Referencia global al manejador de llamada actual

# Configurar logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

AUDIO_DIR = "audio"



def stt_callback_factory(manager):
    def stt_callback(transcript, is_final):
        if manager.is_speaking:
            return  # 🔇 No escuchar mientras está hablando
        if transcript:
            current_time = time.time()
            if current_time - manager.last_final_time < 2.0:
                return
            manager.current_partial = transcript
            manager.last_partial_time = current_time
            if is_final:
                if manager.expecting_number and len(transcript.split()) <= 4:
                    logger.info("🔄 Evitado 'final' porque usuario está dictando el número con pausas")
                    return
                if manager.expecting_name and len(transcript.split()) <= 3:
                    logger.info("🔄 Evitado 'final' porque usuario está diciendo el nombre con pausas")
                    return

                logger.info(f"🎙️ USUARIO (final): {transcript}")
                manager.last_final_time = time.time()

                # 🔁 Cancelamos tarea anterior si sigue corriendo
                if manager.current_gpt_task and not manager.current_gpt_task.done():
                    manager.current_gpt_task.cancel()
                    logger.info("🧹 Tarea anterior de GPT cancelada antes de lanzar nueva.")

                # 🚀 Creamos nueva tarea
                manager.current_gpt_task = asyncio.create_task(
                    manager.process_gpt_response(transcript, manager.websocket)
                )
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
        self.is_speaking = False
        self.conversation_history = []
        self.current_gpt_task = None
        self.expecting_number = False  # 🟡 El usuario está dictando su número
        self.expecting_name = False    # 🟡 El usuario está diciendo el nombre del paciente

    async def handle_twilio_websocket(self, websocket: WebSocket):
        self.websocket = websocket
        await websocket.accept()
        global CURRENT_CALL_MANAGER
        CURRENT_CALL_MANAGER = self

        logger.info("📞 Llamada iniciada. WebSocket aceptado.")

        # 1) Cargar la caché de slots libres al iniciar la llamada
        try:
            load_free_slots_to_cache(days_ahead=90)
            logger.info("✅ Slots libres precargados al iniciar la llamada.")
            load_consultorio_data_to_cache()
            logger.info("✅ Datos del consultorio precargados al iniciar la llamada.")
        except Exception as e:
            logger.error(f"❌ Error cargando slots libres: {str(e)}", exc_info=True)

        try:
            self.stt_streamer = DeepgramSTTStreamer(
                stt_callback_factory(self)
            )
            await self.stt_streamer.start_streaming()
            logger.info("✅ Deepgram STT iniciado correctamente.")
        except Exception as e:
            logger.error(f"❌ Error iniciando Deepgram STT: {e}", exc_info=True)
            await websocket.close(code=1011)
            return

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

            self.conversation_history.append({"role": "user", "content": user_text})
            gpt_response = generate_openai_response(self.conversation_history)

            if gpt_response == "__END_CALL__":
                logger.info("📞 La IA solicitó finalizar la llamada.")
                await self._shutdown()
                return

            self.conversation_history.append({"role": "assistant", "content": gpt_response})

            # 🧠 Detectar intención para control de pausas
            response_lower = gpt_response.lower()
            if "nombre completo del paciente" in response_lower:
                self.expecting_name = True
                self.expecting_number = False
            elif "número de whatsapp" in response_lower:
                self.expecting_number = True
                self.expecting_name = False
            elif "¿es correcto" in response_lower or "¿cuál es el motivo" in response_lower:
                self.expecting_name = False
                self.expecting_number = False

            logger.info(f"🤖 IA: {re.sub(r'http[s]?://\S+', '', gpt_response)}")

            self.is_speaking = True
            audio_bytes = text_to_speech(gpt_response)
            await self._play_audio_bytes(websocket, audio_bytes)
            await asyncio.sleep(len(audio_bytes) / 6400)
            self.is_speaking = False

        except Exception as e:
            logger.error(f"❌ Error procesando respuesta de IA: {e}", exc_info=True)

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
        except Exception as e:
            logger.error(f"❌ Error enviando audio TTS: {e}", exc_info=True)

    async def _shutdown(self):
        logger.info("📴 Cerrando conexión y limpiando recursos...")
        global CURRENT_CALL_MANAGER
        CURRENT_CALL_MANAGER = None

        self.call_ended = True

        if self._silence_task and not self._silence_task.done():
            self._silence_task.cancel()

        if self.current_gpt_task and not self.current_gpt_task.done():
            self.current_gpt_task.cancel()
            logger.info("🧹 Tarea de GPT cancelada al cerrar la llamada.")

        if self.stt_streamer:
            await self.stt_streamer.close()
            await asyncio.sleep(0.1)

        if self.websocket and self.websocket.client_state == WebSocketState.CONNECTED:
            await self.websocket.close()
            self.conversation_history.clear()

        # 2) Borrar la caché de slots cuando termina la llamada
        free_slots_cache.clear()
        last_cache_update = None
        logger.info("🗑️ Caché de slots libres limpiada al terminar la llamada.")
        from consultarinfo import clear_consultorio_data_cache
        clear_consultorio_data_cache()
        logger.info("🗑️ Caché de datos del consultorio limpiada al terminar la llamada.")

        logger.info("✅ Cierre completo del WebSocket Manager.")