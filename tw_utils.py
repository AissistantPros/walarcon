# tw_utils.py
"""
WebSocket manager para Twilio <-> Deepgram <-> GPT‑4o‑mini
----------------------------------------------------------------

"""

import asyncio, base64, json, logging, time
from typing import Optional
from fastapi import WebSocket
from starlette.websockets import WebSocketState

from aiagent import generate_openai_response_main
from buscarslot import load_free_slots_to_cache
from consultarinfo import load_consultorio_data_to_cache
from deepgram_stt_streamer import DeepgramSTTStreamer
from prompt import generate_openai_prompt
from tts_utils import text_to_speech
from utils import get_cancun_time

def set_debug(active: bool = True) -> None:
    level = logging.DEBUG if active else logging.INFO
    for name in ("tw_utils", "aiagent", "buscarslot"):
        logging.getLogger(name).setLevel(level)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

CURRENT_CALL_MANAGER: Optional["TwilioWebSocketManager"] = None
CALL_MAX_DURATION = 600
CALL_SILENCE_TIMEOUT = 30
GOODBYE_PHRASE = "Fue un placer atenderle. ¡Hasta luego!"


class TwilioWebSocketManager:
    def __init__(self) -> None:
        self.current_gpt_task = None
        self.stt_streamer = None
        self.call_ended = False
        self.conversation_history = []
        self.stream_sid = None
        self.websocket = getattr(self, "websocket", None)
        self.is_speaking = False
        self.speaking_lock = asyncio.Lock()
        now = self._now()
        self.stream_start_time = now
        self.last_final_ts = now
        self.last_activity_ts = now
        self.finales_acumulados: list[str] = []
        self.temporizador_en_curso: Optional[asyncio.Task] = None
        





    def _now(self) -> float:
        return time.perf_counter()






    async def handle_twilio_websocket(self, websocket: WebSocket):
        self.websocket = websocket
        await websocket.accept()
        global CURRENT_CALL_MANAGER; CURRENT_CALL_MANAGER = self
        logger.info("📞 Llamada iniciada")
        logger.debug("🧽 Limpieza inicial de acumulador y timestamp")

        try:
            load_free_slots_to_cache(90)
            load_consultorio_data_to_cache()
        except Exception as e:
            logger.warning("⚠️ Precarga falló: %s", e, exc_info=True)

        try:
            self.stt_streamer = DeepgramSTTStreamer(self._stt_callback)
            await self.stt_streamer.start_streaming()
        except Exception as e:
            logger.error("❌ Deepgram no arrancó: %s", e, exc_info=True)
            await websocket.close(code=1011)
            return

        asyncio.create_task(self._monitor_call_timeout())

        try:
            while True:
                raw = await websocket.receive_text()
                data = json.loads(raw)
                evt = data.get("event")
                if evt == "start":
                    self.stream_sid = data.get("streamSid", "")
                    await self._play_audio_bytes(text_to_speech(self._greeting()))
                elif evt == "media":
                    if not self.is_speaking:
                        payload = data["media"].get("payload")
                        if payload:
                            await self.stt_streamer.send_audio(base64.b64decode(payload))
                elif evt == "stop":
                    logger.info("🛑 Twilio envió stop")
                    break
        except Exception as e:
            logger.error("❌ WebSocket error: %s", e, exc_info=True)
        finally:
            await self._shutdown()











     # ────────────────────────────────────────────────────────────
    # 🎙️ CALLBACK DEEPGRAM
    # ────────────────────────────────────────────────────────────
    def _stt_callback(self, transcript: str, is_final: bool):
        # 🔊 Sólo imprimimos si es final True
        if is_final:
            logger.info(f"📥 Final recibido (DG): '{transcript.strip()}'")

        # 👉 De aquí para abajo NO filtramos: mantenemos la lógica original
        if not (is_final and transcript and transcript.strip()):
            return

        self.last_activity_ts = self._now()
        self.finales_acumulados.append(transcript.strip())

        if self.temporizador_en_curso and not self.temporizador_en_curso.done():
            self.temporizador_en_curso.cancel()

        self.temporizador_en_curso = asyncio.create_task(self._esperar_y_mandar_finales())






    async def _esperar_y_mandar_finales(self):
        try:
            logger.debug("⏳ Temporizador esperando 1.1 segundos")
            await asyncio.sleep(4)  # Espera el tiempo necesario para acumular finales
            elapsed = self._now() - self.last_activity_ts
            logger.debug(f"⌛ Tiempo desde último final: {elapsed:.4f}s")

            # Verifica si la llamada ya terminó
            if self.call_ended:
                logger.debug("⚠️ Llamada finalizada. No se enviará nada a GPT.")
                self.finales_acumulados.clear()
                return

            # Si ha pasado el tiempo de espera y hay finales acumulados
            if elapsed >= 1.0 and self.finales_acumulados:
                # Unir los mensajes acumulados
                mensaje = " ".join(self.finales_acumulados).replace("\n", " ").strip()
                logger.debug(f"📤 Enviando a GPT: '{mensaje}'")

                # Limpiar la lista de finales acumulados
                self.finales_acumulados.clear()

                # Cancelar cualquier tarea previa de GPT
                if self.current_gpt_task and not self.current_gpt_task.done():
                    self.current_gpt_task.cancel()

                # Enviar el mensaje acumulado a GPT
                logger.info(f"🌐 Enviando mensaje acumulado a GPT: '{mensaje}'")
                self.current_gpt_task = asyncio.create_task(self.process_gpt_response(mensaje))

        except asyncio.CancelledError:
            logger.debug("🛑 Temporizador cancelado antes de completarse")
        except Exception as e:
            logger.error(f"❌ Error en acumulador: {e}")












    async def process_gpt_response(self, user_text: str):
        if self.call_ended or not self.websocket or self.websocket.client_state != WebSocketState.CONNECTED:
            return
        self.last_final_ts = self._now()

        # Log para confirmar que el mensaje acumulado llegó a GPT
        logger.info(f"🗣️ Mensaje recibido en GPT: '{user_text}'")
        self.conversation_history.append({"role": "user", "content": f"[ES] {user_text}"})

        reply = await generate_openai_response_main(
            generate_openai_prompt(self.conversation_history),
            model="gpt-4.1-mini"
        )

        if reply.strip() == "__END_CALL__":
            logger.info("🚪 Protocolo de cierre activado por IA")
            despedida_ya_dicha = any(
                any(k in m["content"].lower() for k in ("gracias", "hasta luego", "placer atenderle", "excelente día"))
                for m in self.conversation_history if m["role"] == "assistant"
            )
            if not despedida_ya_dicha:
                frase = GOODBYE_PHRASE
                await self._play_audio_bytes(text_to_speech(frase))
                self.conversation_history.append({"role": "assistant", "content": frase})

            if self.stt_streamer:
                await self.stt_streamer.close()

            await asyncio.sleep(4)
            await self._shutdown()
            return

        self.conversation_history.append({"role": "assistant", "content": reply})
        logger.info(f"🤖 Respuesta de GPT: {reply}")

        await self._play_audio_bytes(text_to_speech(reply))
        await asyncio.sleep(0.2)
        await self._send_silence_chunk()











    async def _play_audio_bytes(self, audio_data: bytes):
        """
        Envía el audio a Twilio y, al terminar, procesa cualquier final que
        Deepgram haya enviado mientras la IA hablaba.
        """
        if not audio_data or not self.websocket or self.websocket.client_state != WebSocketState.CONNECTED:
            return

        self.tts_start_time = self._now()          # 🕒 marca inicio TTS
        async with self.speaking_lock:
            self.is_speaking = True

        # ───── stream como antes ───────────────────────────
        chunk_size = 512
        for offset in range(0, len(audio_data), chunk_size):
            chunk = audio_data[offset: offset + chunk_size]
            await self.websocket.send_text(json.dumps({
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": base64.b64encode(chunk).decode()},
            }))
            await asyncio.sleep(chunk_size / 8000.0)

        async with self.speaking_lock:
            self.is_speaking = False

        # reactiva STT un segundo antes de acabar
        if self.stt_streamer:
            await self._reactivate_stt_after(0.0)







    async def _reactivate_stt_after(self, delay: float):
        await asyncio.sleep(delay)
        if self.stt_streamer and not self.call_ended:
            await self._send_silence_chunk()






    async def _send_silence_chunk(self):
        if self.stt_streamer:
            try:
                await self.stt_streamer.send_audio(b"\xff" * 320)
            except Exception:
                pass






    def _greeting(self):
        now = get_cancun_time(); h = now.hour; m = now.minute
        if 3 <= h < 12:
            return "¡Buenos días!, Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"
        if h >= 20 or h < 3 or (h == 19 and m >= 30):
            return "¡Buenas noches!, Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"
        return "¡Buenas tardes!, Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"






    async def _monitor_call_timeout(self):
        while not self.call_ended:
            await asyncio.sleep(2)
            now = self._now()
            if now - self.stream_start_time >= CALL_MAX_DURATION:
                logger.info("⏰ Duración máxima excedida")
                await self._shutdown(); return
            if now - self.last_activity_ts >= CALL_SILENCE_TIMEOUT:
                logger.info("🛑 Silencio prolongado (sin actividad humana)")
                await self._shutdown(); return

            await self._send_silence_chunk()






    async def _shutdown(self):
        if self.call_ended:
            logger.debug("⚠️ Llamada ya finalizada, cancelando lógica de acumulador")
            return
        self.call_ended = True
        logger.info("🔻 Cuelga llamada")
        if self.stt_streamer:
            await self.stt_streamer.close()
        if self.websocket and self.websocket.client_state == WebSocketState.CONNECTED:
            await self.websocket.close()
        self.conversation_history.clear()
