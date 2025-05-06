# tw_utils.py
"""
WebSocket manager para Twilio <-> Deepgram <-> GPT‑4o‑mini
----------------------------------------------------------------
Versión optimizada mayo 2025
• Un solo sistema de acumulación robusto.
• Compatible con modo teléfono y tiempo de gracia variable.
• Sin respuestas duplicadas ni superposiciones.
"""

import asyncio, base64, json, logging, time, re
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
GRACE_MS_NORMAL = 0.7
GRACE_MS_PHONE = 3.5
GOODBYE_PHRASE = "Fue un placer atenderle. ¡Hasta luego!"
MIN_RESTART_INTERVAL = 0.3

class TwilioWebSocketManager:
    def __init__(self) -> None:
        self.grace_ms = GRACE_MS_NORMAL
        self.phone_attempts = 0
        self.final_accumulated = []
        self.final_timer_task = None
        self.current_gpt_task = None
        self.stt_streamer = None
        self.call_ended = False
        self.accumulating_mode = False
        self.conversation_history = []
        self.stream_sid = None
        self.websocket = getattr(self, "websocket", None)
        self.is_speaking = False
        self.speaking_lock = asyncio.Lock()
        now = self._now()
        self.stream_start_time = now
        self.last_final_ts = now

    def _now(self) -> float:
        return time.perf_counter()

    async def handle_twilio_websocket(self, websocket: WebSocket):
        self.websocket = websocket
        await websocket.accept()
        global CURRENT_CALL_MANAGER; CURRENT_CALL_MANAGER = self
        logger.info("📞 Llamada iniciada")

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







    def _stt_callback(self, transcript: str, is_final: bool):
        if not transcript or not is_final:
            return

        now = self._now()
        self.final_accumulated.append(transcript.strip())

        # Reinicio inteligente del cronómetro solo si ya pasó suficiente tiempo
        if (
            self.final_timer_task
            and not self.final_timer_task.done()
            and (now - self.last_final_ts) >= MIN_RESTART_INTERVAL
        ):
            self.final_timer_task.cancel()

        self.last_final_ts = now

        # Si no hay cronómetro activo, o se canceló, arráncalo
        if not self.final_timer_task or self.final_timer_task.done():
            logger.debug("🕓 Consolidación con gracia de %.1f s (modo teléfono: %s)",
                        self.grace_ms, self.accumulating_mode)
            self.final_timer_task = asyncio.create_task(self._cronometro_de_gracia())









    async def _cronometro_de_gracia(self):
        try:
            await asyncio.sleep(self.grace_ms)
            logger.debug("✅ Esperé %.2f s completos, procedo a consolidar", self.grace_ms)

        except asyncio.CancelledError:
            return  # Se reinició el cronómetro

        if self.final_timer_task != asyncio.current_task():
            logger.debug("⛔ Tarea antigua ignorada.")
            return

        if not self.final_accumulated:
            logger.debug("⚠️ Lista vacía, nada que enviar.")
            return

        # 💥 Agrega este log:
        logger.debug("🕓 Consolidación con gracia de %.1f segundos (modo teléfono: %s)", self.grace_ms, self.accumulating_mode)

        texto = " ".join(self.final_accumulated).strip()
        self.final_accumulated.clear()

        logger.debug("🟢 Frase consolidada enviada a IA → %s", texto)

        if self.current_gpt_task and not self.current_gpt_task.done():
            self.current_gpt_task.cancel()

        self.current_gpt_task = asyncio.create_task(self.process_gpt_response(texto))





    def _activate_phone_mode(self):
        if self.accumulating_mode:
            return
        logger.info("📞 Modo teléfono ON (grace_ms ahora = 3.5)")
        self.accumulating_mode = True
        self.grace_ms = GRACE_MS_PHONE






    def _exit_phone_mode(self):
        if not self.accumulating_mode:
            return
        logger.info("📞 Modo teléfono OFF (grace_ms ahora = 0.7)")
        self.accumulating_mode = False
        self.grace_ms = GRACE_MS_NORMAL




    async def _activate_phone_mode_after_audio(self):
        logger.debug("⏳ Esperando a que termine el TTS para activar modo teléfono...")
        while self.is_speaking and not self.call_ended:
            await asyncio.sleep(0.1)
        logger.debug("✅ TTS terminó. Activando modo teléfono.")
        self._activate_phone_mode()





    async def process_gpt_response(self, user_text: str):
        if self.call_ended or not self.websocket or self.websocket.client_state != WebSocketState.CONNECTED:
            return

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
        logger.info("🤖 IA: %s", reply)

        if any(k in reply.lower() for k in (
            "número de whatsapp", "número de teléfono", "compartir el número",
            "me puede compartir el número de whatsapp para enviarle la confirmación"
        )):
            logger.info("🟠 Se detectó que IA solicitó número. Activando modo teléfono después del audio.")

            asyncio.create_task(self._activate_phone_mode_after_audio())

        if "cuál es el motivo de la consulta" in reply.lower():
            self._exit_phone_mode()

        await self._play_audio_bytes(text_to_speech(reply))
        await asyncio.sleep(0.2)
        await self._send_silence_chunk()

    async def _play_audio_bytes(self, audio_data: bytes):
        if not audio_data or not self.websocket or self.websocket.client_state != WebSocketState.CONNECTED:
            return

        duration = len(audio_data) / 8000.0
        async with self.speaking_lock:
            self.is_speaking = True

        if self.stt_streamer:
            delay = max(0.0, duration - 1.0)
            asyncio.create_task(self._reactivate_stt_after(delay))

        chunk_size = 512
        offset = 0
        while offset < len(audio_data) and not self.call_ended:
            chunk = audio_data[offset: offset + chunk_size]
            offset += chunk_size
            await self.websocket.send_text(json.dumps({
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": base64.b64encode(chunk).decode()},
            }))
            await asyncio.sleep(chunk_size / 8000.0)

        async with self.speaking_lock:
            self.is_speaking = False

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
            if now - self.last_final_ts >= CALL_SILENCE_TIMEOUT:
                logger.info("🛑 Silencio prolongado")
                await self._shutdown(); return
            await self._send_silence_chunk()

    async def _shutdown(self):
        if self.call_ended:
            return
        self.call_ended = True
        logger.info("🔻 Cuelga llamada")
        if self.stt_streamer:
            await self.stt_streamer.close()
        if self.websocket and self.websocket.client_state == WebSocketState.CONNECTED:
            await self.websocket.close()
        self.final_accumulated.clear()
        self.conversation_history.clear()
        self.final_timer_task = None
