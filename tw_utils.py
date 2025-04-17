# tw_utils.py

"""
Este archivo gestiona toda la lógica de llamada con Twilio WebSocket.

- Cada vez que el usuario habla, se envía:
  - La hora actual en Cancún como system_message temporal.
  - El mensaje del usuario.
  - El historial anterior sin system_messages.

- La IA puede consultar también la hora con la tool get_cancun_time si lo necesita.

- El historial real mantiene solo las intervenciones de usuario e IA.

- La lógica de acumulación permite recoger números largos como teléfonos sin cortes.

Autor: Esteban Reyna / Aissistants Pro
"""


import json
import base64
import time
import asyncio
import logging
from fastapi import WebSocket
from starlette.websockets import WebSocketState
from consultarinfo import load_consultorio_data_to_cache
from deepgram_stt_streamer import DeepgramSTTStreamer
from aiagent import generate_openai_response_main
from tts_utils import text_to_speech
from utils import get_cancun_time
from buscarslot import load_free_slots_to_cache
from prompt import generate_openai_prompt

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

CURRENT_CALL_MANAGER = None

CALL_MAX_DURATION = 600
CALL_SILENCE_TIMEOUT = 30

class TwilioWebSocketManager:
    def __init__(self):
        self.accumulating_timeout_general = 1.0
        self.accumulating_timeout_phone = 2.5
        self.accumulating_timer_task = None
        self._reset_all_state()

    def _reset_all_state(self):
        logger.info("🧼 Reiniciando TODAS las variables internas del sistema.")
        self.call_ended = False
        self.conversation_history = []
        self.current_language = "es"
        self.expecting_number = False
        self.expecting_name = False

        self.accumulating_mode = False
        self.accumulated_transcripts = []
        self._cancel_accumulating_timer()

        self.current_gpt_task = None
        self.stt_streamer = None
        self.is_speaking = False
        self.stream_sid = None
    # 👇 NO BORRAR el websocket aquí, ya está asignado externamente
    # self.websocket = None  ❌ ¡No lo borres o pierdes conexión!
        now = time.time()
        self.stream_start_time = now
        self.last_partial_time = now
        self.last_final_time = now

    async def handle_twilio_websocket(self, websocket: WebSocket):
        self.websocket = websocket
        await websocket.accept()
        self._reset_all_state()

        global CURRENT_CALL_MANAGER
        CURRENT_CALL_MANAGER = self

        logger.info("📞 Llamada iniciada.")
        

        try:
            load_free_slots_to_cache(90)
            load_consultorio_data_to_cache()
        except Exception as e:
            logger.error(f"❌ Error precargando datos: {e}", exc_info=True)

        try:
            self.stt_streamer = DeepgramSTTStreamer(self._stt_callback)
            await self.stt_streamer.start_streaming()
        except Exception as e:
            logger.error(f"❌ Error iniciando STT: {e}", exc_info=True)
            await websocket.close(code=1011)
            return

        asyncio.create_task(self._monitor_call_timeout())

        try:
            while True:
                raw_msg = await websocket.receive_text()
                data = json.loads(raw_msg)
                event_type = data.get("event")

                if event_type == "start":
                    self.stream_sid = data.get("streamSid", "")
                    saludo = self._get_greeting_by_time()
                    saludo_audio = text_to_speech(saludo)
                    await self._play_audio_bytes(saludo_audio)

                elif event_type == "media":
                    if self.is_speaking:
                        continue
                    payload = data["media"].get("payload")
                    if payload:
                        audio_chunk = base64.b64decode(payload)
                        await self.stt_streamer.send_audio(audio_chunk)

                elif event_type == "stop":
                    logger.info("🛑 Evento 'stop' recibido desde Twilio.")
                    break

        except Exception as e:
            logger.error(f"❌ WebSocket error: {e}", exc_info=True)
        finally:
            await self._shutdown()









    async def _send_silence_chunk(self):
        if self.stt_streamer:
            try:
                silence = b'\xff' * 320
                await self.stt_streamer.send_audio(silence)
            except Exception as e:
                logger.warning(f"⚠️ Error al enviar silencio: {e}")














    def _get_greeting_by_time(self):
        now = get_cancun_time()
        hour, minute = now.hour, now.minute
        if 3 <= hour < 12:
            return "¡Buenos días!, Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"
        elif hour >= 20 or hour < 3 or (hour == 19 and minute >= 30):
            return "¡Buenas noches!, Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"
        else:
            return "¡Buenas tardes!, Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"












    def _stt_callback(self, transcript, is_final):
        if not transcript:
            return
        self.last_partial_time = time.time()
        if is_final:
            logger.info(f"🎙️ USUARIO (final): {transcript}")
            self.last_final_time = time.time()
        
            if self.current_gpt_task and not self.current_gpt_task.done():
                self.current_gpt_task.cancel()
                logger.info("🧹 GPT anterior cancelado.")
        
        # 🔕 Acumulación desactivada temporalmente
            self.current_gpt_task = asyncio.create_task(
                self.process_gpt_response(transcript)
            )












    def _activate_accumulating_mode(self):
        logger.info("🛑 Acumulación desactivada temporalmente. Ignorando activación.")
        return









    def _accumulate_transcript(self, transcript):
        logger.info("🛑 Acumulación desactivada temporalmente. Ignorando fragmento.")
        return










    def _start_accumulating_timer(self, phone_mode=False):
        loop = asyncio.get_event_loop()
        timeout = self.accumulating_timeout_phone if phone_mode else self.accumulating_timeout_general
        self.accumulating_timer_task = loop.create_task(self._accumulating_timer(timeout))
        logger.info(f"⏳ Temporizador iniciado ({timeout}s).")









    def _cancel_accumulating_timer(self):
        if self.accumulating_timer_task and not self.accumulating_timer_task.done():
            self.accumulating_timer_task.cancel()
            self.accumulating_timer_task = None











    async def _accumulating_timer(self, timeout):
        try:
            await asyncio.sleep(timeout)
            logger.info("🟠 Tiempo agotado. Flusheando...")
            self._flush_accumulated_transcripts()
        except asyncio.CancelledError:
            logger.debug("🔁 Temporizador de acumulación cancelado (nuevo fragmento llegó).")











    def _flush_accumulated_transcripts(self):
        logger.info("🛑 Acumulación desactivada temporalmente. Ignorando flush.")
        return
















    async def process_gpt_response(self, user_text: str):
        if self.call_ended or not self.websocket or self.websocket.client_state != WebSocketState.CONNECTED:
            return

        user_lang = "ES"
        self.current_language = user_lang

        user_input = {"role": "user", "content": f"[{user_lang}] {user_text}"}
        messages_for_gpt = generate_openai_prompt(self.conversation_history + [user_input])
       

        model = "gpt-4.1-mini"
        logger.info(f"⌛ Se utilizará el modelo: {model} para el texto: {user_text}")

        gpt_response = await generate_openai_response_main(messages_for_gpt, model=model)

        if gpt_response == "__END_CALL__":
            await self._shutdown()
            return

        self.conversation_history.append(user_input)
        self.conversation_history.append({"role": "assistant", "content": gpt_response})

        logger.info(f"🤖 IA (texto completo): {gpt_response}")
        resp_lower = gpt_response.lower()

        if "número de whatsapp" in resp_lower:
            self._activate_accumulating_mode()
            self.expecting_number = True
            self.expecting_name = False
        elif any(kw in resp_lower for kw in ["¿es correcto", "¿cuál es el motivo", "¿confirmamos"]):
            self.expecting_number = False
            self.expecting_name = False

        if "fue un placer atenderle. que tenga un excelente día. ¡hasta luego!" in resp_lower:
            logger.info("🧼 Frase de cierre detectada. Reproduciendo y terminando llamada.")
            self.is_speaking = True
            tts_audio = text_to_speech(gpt_response)
            await self._play_audio_bytes(tts_audio)
            await asyncio.sleep(5)
            self.is_speaking = False
            await self._shutdown()
            return

        self.is_speaking = True
        tts_audio = text_to_speech(gpt_response)
        await self._play_audio_bytes(tts_audio)
        await asyncio.sleep(len(tts_audio) / 6400)
        self.is_speaking = False









    async def _play_audio_bytes(self, audio_data: bytes):
        if not audio_data:
            logger.warning("❌ No hay audio para reproducir.")
            return
        if not self.websocket or self.websocket.client_state != WebSocketState.CONNECTED:
            logger.warning("❌ WebSocket no conectado. No se puede enviar audio.")
            return

        logger.info(f"📤 Enviando audio a Twilio ({len(audio_data)} bytes)...")
        
        chunk_size = 1024
        total_len = len(audio_data)
        offset = 0
        while offset < total_len and not self.call_ended:
            chunk = audio_data[offset:offset+chunk_size]
            offset += chunk_size
            base64_chunk = base64.b64encode(chunk).decode('utf-8')
            message = {
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": base64_chunk}
            }
            try:
                await self.websocket.send_text(json.dumps(message))
                await asyncio.sleep(0.03)
            except Exception as e:
                logger.error(f"Error enviando audio: {e}")
                break










    async def _shutdown(self):
        if self.call_ended:
            return
        self.call_ended = True
        logger.info("🔻 Terminando la llamada...")
        if self.stt_streamer:
            await self.stt_streamer.close()
        if self.websocket and self.websocket.client_state == WebSocketState.CONNECTED:
            await self.websocket.close()
        logger.info("🧹 Ejecutando limpieza total de variables tras finalizar la llamada.")
        self._reset_all_state()









    async def _monitor_call_timeout(self):
        while not self.call_ended:
            await asyncio.sleep(2)
            elapsed_call = time.time() - self.stream_start_time
            if elapsed_call >= CALL_MAX_DURATION:
                logger.info("⏰ Tiempo máximo de llamada excedido.")
                await self._shutdown()
                return
            silence_elapsed = time.time() - self.last_final_time
            if silence_elapsed >= CALL_SILENCE_TIMEOUT:
                logger.info("🛑 Silencio prolongado. Terminando llamada.")
                await self._shutdown()
                return
            await self._send_silence_chunk()

