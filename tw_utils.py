# tw_utils.py

import json
import base64
import time
import asyncio
import logging
from fastapi import WebSocket
from starlette.websockets import WebSocketState
from consultarinfo import load_consultorio_data_to_cache
from deepgram_stt_streamer import DeepgramSTTStreamer
from aiagent import generate_openai_response
from tts_utils import text_to_speech
from utils import get_cancun_time
from buscarslot import load_free_slots_to_cache, free_slots_cache, last_cache_update
from difflib import SequenceMatcher
import re

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

CURRENT_CALL_MANAGER = None
AUDIO_DIR = "audio"
CALL_MAX_DURATION = 600
CALL_SILENCE_TIMEOUT = 30


def clean_buffer(new_transcript, buffer):
    similarity = SequenceMatcher(None, new_transcript, buffer).ratio()
    return buffer if similarity > 0.8 else f"{buffer} {new_transcript}"


def stt_callback_factory(manager):
    def stt_callback(transcript, is_final):
        if manager.is_speaking or not transcript:
            return

        current_time = time.time()
        manager.last_partial_time = current_time

        if manager.expecting_name or manager.expecting_number:
            manager.current_partial_buffer = clean_buffer(transcript.strip(), manager.current_partial_buffer)
            manager.current_partial = manager.current_partial_buffer.strip()

            if current_time - manager.last_partial_time > 4.5 and manager.current_partial:
                logger.info("🔔 Silencio prolongado. Forzando final.")
                transcript = manager.current_partial
                is_final = True
                manager.current_partial = ""
                manager.current_partial_buffer = ""
            elif not is_final:
                return
        else:
            if current_time - manager.last_final_time < 2.0:
                return
            manager.current_partial = transcript

        if is_final:
            if manager.expecting_number and len(transcript.split()) <= 4:
                logger.info("🔄 Número incompleto sin silencio suficiente")
                return
            elif manager.expecting_name and len(transcript.split()) <= 3:
                logger.info("🔄 Nombre incompleto sin silencio suficiente")
                return

            logger.info(f"🎙️ USUARIO (final): {transcript}")
            manager.last_final_time = current_time

            if manager.current_gpt_task and not manager.current_gpt_task.done():
                manager.current_gpt_task.cancel()
                logger.info("🧹 GPT anterior cancelado.")

            manager.current_gpt_task = asyncio.create_task(
                manager.process_gpt_response(transcript, manager.websocket)
            )

    return stt_callback


def get_greeting_by_time():
    now = get_cancun_time()
    if 3 <= now.hour < 12:
        return "Buenos días, soy Dany, la asistente virtual del Dr. Alarcón. ¿En qué puedo ayudarle?"
    elif now.hour >= 20 or now.hour < 3 or (now.hour == 19 and now.minute >= 30):
        return "Buenas noches, soy Dany, la asistente virtual del Dr. Alarcón. ¿En qué puedo ayudarle?"
    return "Buenas tardes, soy Dany, la asistente virtual del Dr. Alarcón. ¿En qué puedo ayudarle?"


class TwilioWebSocketManager:
    def __init__(self):
        self.call_ended = False
        self.stream_sid = None
        self.stt_streamer = None
        self.stream_start_time = time.time()
        self.current_partial = ""
        self.current_partial_buffer = ""
        self.last_partial_time = 0.0
        self.last_final_time = time.time()
        self._silence_task = None
        self.websocket = None
        self.is_speaking = False
        self.conversation_history = []
        self.current_gpt_task = None
        self.expecting_number = False
        self.expecting_name = False

    async def handle_twilio_websocket(self, websocket: WebSocket):
        self.websocket = websocket
        await websocket.accept()
        global CURRENT_CALL_MANAGER
        CURRENT_CALL_MANAGER = self

        logger.info("📞 Llamada iniciada.")

        try:
            load_free_slots_to_cache(90)
            load_consultorio_data_to_cache()
        except Exception as e:
            logger.error(f"❌ Error precargando datos: {e}", exc_info=True)

        try:
            self.stt_streamer = DeepgramSTTStreamer(stt_callback_factory(self))
            await self.stt_streamer.start_streaming()
        except Exception as e:
            logger.error(f"❌ Error iniciando STT: {e}", exc_info=True)
            await websocket.close(code=1011)
            return

        asyncio.create_task(self._monitor_call_timeout())

        try:
            while True:
                data = json.loads(await websocket.receive_text())
                if data.get("event") == "start":
                    self.stream_sid = data.get("streamSid")
                    saludo = get_greeting_by_time()
                    await self._play_audio_bytes(websocket, text_to_speech(saludo))
                elif data.get("event") == "media":
                    await self.stt_streamer.send_audio(base64.b64decode(data["media"].get("payload")))
                elif data.get("event") == "stop":
                    break
        except Exception as e:
            logger.error(f"❌ WebSocket error: {e}", exc_info=True)
        finally:
            await self._shutdown()

    async def _monitor_call_timeout(self):
        while not self.call_ended:
            await asyncio.sleep(5)
            now = time.time()
            if now - self.stream_start_time > CALL_MAX_DURATION:
                logger.info("⏱️ Tiempo máximo alcanzado. Terminando llamada.")
                await self._shutdown()
                break
            elif now - self.last_final_time > CALL_SILENCE_TIMEOUT:
                logger.info("🤫 Silencio prolongado. Terminando llamada.")
                await self._shutdown()
                break

    async def process_gpt_response(self, user_text: str, websocket: WebSocket):
        if self.call_ended or websocket.client_state != WebSocketState.CONNECTED:
            return

        self.conversation_history.append({"role": "user", "content": user_text})
        gpt_response = generate_openai_response(self.conversation_history)

        if gpt_response == "__END_CALL__":
            await self._shutdown()
            return

        self.conversation_history.append({"role": "assistant", "content": gpt_response})

        lower = gpt_response.lower()
        if "nombre completo del paciente" in lower:
            self.expecting_name = True
            self.expecting_number = False
        elif "número de whatsapp" in lower:
            self.expecting_number = True
            self.expecting_name = False
        elif any(kw in lower for kw in ["¿es correcto", "¿cuál es el motivo", "¿confirmamos"]):
            self.expecting_name = False
            self.expecting_number = False

        if "fue un placer atenderle. que tenga un excelente día. ¡hasta luego!" in lower:
            logger.info("🧼 Frase de cierre detectada. Terminando llamada.")
            await self._shutdown()
            return

        self.is_speaking = True
        audio_bytes = text_to_speech(gpt_response)
        await self._play_audio_bytes(websocket, audio_bytes)
        await asyncio.sleep(len(audio_bytes) / 6400)
        self.is_speaking = False

    async def _play_audio_bytes(self, websocket: WebSocket, audio_bytes: bytes):
        if not self.stream_sid or self.call_ended or websocket.client_state != WebSocketState.CONNECTED:
            return
        try:
            await websocket.send_text(json.dumps({
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": base64.b64encode(audio_bytes).decode("utf-8")},
            }))
        except Exception as e:
            logger.error(f"❌ Error TTS: {e}", exc_info=True)

    async def _shutdown(self):
        global CURRENT_CALL_MANAGER
        CURRENT_CALL_MANAGER = None
        self.call_ended = True

        if self.current_gpt_task and not self.current_gpt_task.done():
            self.current_gpt_task.cancel()

        if self.stt_streamer:
            await self.stt_streamer.close()

        if self.websocket and self.websocket.client_state == WebSocketState.CONNECTED:
            await self.websocket.close()

        self.conversation_history.clear()
        free_slots_cache.clear()
        last_cache_update = None
        from consultarinfo import clear_consultorio_data_cache
        clear_consultorio_data_cache()

        logger.info("✅ Llamada finalizada y recursos limpiados.")
