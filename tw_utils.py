import asyncio, base64, json, logging, time, re
from typing import Optional, Set

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from aiagent import generate_openai_response_main
from buscarslot import load_free_slots_to_cache
from consultarinfo import load_consultorio_data_to_cache
from deepgram_stt_streamer import DeepgramSTTStreamer
from prompt import generate_openai_prompt
from tts_utils import text_to_speech
from utils import get_cancun_time

# ────────────────────────────────────────────────────────────────
# CONFIG LOGGING
# ────────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# ────────────────────────────────────────────────────────────────
# CONSTANTES GLOBALES
# ────────────────────────────────────────────────────────────────
CALL_MAX_DURATION = 600                # 10 min
CALL_SILENCE_TIMEOUT = 30              # 30 s
GOODBYE_PHRASE = "Fue un placer atenderle. Que tenga un excelente día. ¡Hasta luego!"

# =====================================================================
# CLASE PRINCIPAL
# =====================================================================
class TwilioWebSocketManager:
    """Gestiona la sesión RTC completa con Twilio, Deepgram, GPT‑4 y ElevenLabs."""

    # ────────────────────────────────────────────────────────────
    # 🚧 CONSTRUCTOR & RESET
    # ────────────────────────────────────────────────────────────
    def __init__(self) -> None:
        # parámetros de STT / gracia
        self.grace_ms = 0.7                  # ventana de gracia para unir partials
        self.accumulating_timeout_phone = 3.5
        # contenedores y tareas
        self.active_tasks: Set[asyncio.Task] = set()
        self.speaking_lock = asyncio.Lock()
        self._reset_all_state()

    # util perf
    def _now(self) -> float:
        return time.perf_counter()

    def _reset_all_state(self):
        logger.debug("🧼 Reset interno completo")
        # conversación
        self.call_ended = False
        self.conversation_history = []
        self.pending_final: Optional[str] = None
        # modo teléfono
        self.accumulating_mode = False
        self.accumulated_transcripts = []
        self.phone_attempts = 0
        # tareas variables
        self.accumulating_timer_task = None
        self.final_grace_task = None
        self.current_gpt_task = None
        self.reactivate_stt_task = None
        # objetos runtime
        self.stt_streamer = None
        self.websocket: Optional[WebSocket] = None
        self.stream_sid = None
        # tiempos
        now = self._now()
        self.stream_start_time = now
        self.last_final_ts = now
        self._dg_prev_final_ts = now
        self._dg_first_final_ts = None
        # flags
        self.is_speaking = False

    # ────────────────────────────────────────────────────────────
    # 📞  MANEJO WEBSOCKET TWILIO
    # ────────────────────────────────────────────────────────────
    async def handle_twilio_websocket(self, websocket: WebSocket):
        """Punto de entrada principal: atiende la conexión de Twilio."""
        self.websocket = websocket
        await websocket.accept()
        self._reset_all_state()
        logger.info("📞 Nueva llamada entrante desde Twilio.")

        # precarga datos de negocio en background (no bloquear loop)
        await asyncio.gather(
            asyncio.to_thread(load_free_slots_to_cache, 90),
            asyncio.to_thread(load_consultorio_data_to_cache),
        )

        # iniciar Deepgram
        self.stt_streamer = DeepgramSTTStreamer(self._stt_callback)
        try:
            await asyncio.wait_for(self.stt_streamer.start_streaming(), timeout=10)
        except asyncio.TimeoutError:
            logger.error("❌ Timeout iniciando Deepgram")
            await self._shutdown()
            return

        # watchdog
        self._track_task(asyncio.create_task(self._monitor_call_timeout()))

        try:
            while True:
                raw = await websocket.receive_text()
                data = json.loads(raw)
                evt = data.get("event")
                if evt == "start":
                    self.stream_sid = data.get("streamSid")
                    await self._play_audio_bytes(text_to_speech(self._greeting()))
                elif evt == "media":
                    if not self.is_speaking:
                        payload = base64.b64decode(data["media"].get("payload"))
                        if payload and self.stt_streamer:
                            await self.stt_streamer.send_audio(payload)
                elif evt == "stop":
                    logger.info("🛑 Twilio envió stop")
                    break
        except Exception as e:
            logger.exception("Error WebSocket Twilio: %s", e)
        finally:
            await self._shutdown()

    # ────────────────────────────────────────────────────────────
    # 🔊  AUDIO & SALUDO
    # ────────────────────────────────────────────────────────────
    def _greeting(self):
        now = get_cancun_time(); h = now.hour
        if 3 <= h < 12:
            return "¡Buenos días!, Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"
        if h >= 20 or h < 3:
            return "¡Buenas noches!, Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"
        return "¡Buenas tardes!, Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"

    async def _play_audio_bytes(self, audio_data: bytes):
        if not audio_data or not self.websocket or self.websocket.client_state != WebSocketState.CONNECTED:
            return
        duration = len(audio_data) / 8000.0  # 8 kHz μ‑law
        async with self.speaking_lock:
            self.is_speaking = True
        # desactiva STT y prográmalo para reactivarse justo antes de acabar el TTS
        if self.stt_streamer:
            delay = max(0, duration - 0.5)
            self.reactivate_stt_task = asyncio.create_task(self._reactivate_stt_after(delay))
            self._track_task(self.reactivate_stt_task)

        chunk_size = 512
        offset = 0
        while offset < len(audio_data) and not self.call_ended:
            chunk = audio_data[offset:offset+chunk_size]
            offset += chunk_size
            await self.websocket.send_text(json.dumps({
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": base64.b64encode(chunk).decode()},
            }))
            await asyncio.sleep(chunk_size / 8000.0)  # 8 kHz

        async with self.speaking_lock:
            self.is_speaking = False

    async def _reactivate_stt_after(self, delay: float):
        await asyncio.sleep(delay)
        async with self.speaking_lock:
            self.is_speaking = False

    async def _send_silence_chunk(self):
        if self.stt_streamer:
            try:
                await self.stt_streamer.send_audio(b"\xff" * 320)
            except Exception:
                pass

    # ────────────────────────────────────────────────────────────
    # 🎙️  CALLBACK DEEPGRAM
    # ────────────────────────────────────────────────────────────
    def _stt_callback(self, transcript: str, is_final: bool):
        if not transcript or self.call_ended:
            return
        now = self._now()
        if is_final:
            self.last_final_ts = now

        # modo acumulación (teléfono)
        if self.accumulating_mode:
            if is_final:
                self.accumulated_transcripts.append(transcript.strip())
                self._restart_accumulating_timer()
            return

        # pending_final logic
        if is_final:
            self.pending_final = (self.pending_final + " " if self.pending_final else "") + transcript.strip()
            loop = asyncio.get_event_loop()
            if self.final_grace_task and not self.final_grace_task.done():
                self.final_grace_task.cancel()
            self.final_grace_task = loop.create_task(self._commit_final_after_grace())
            self._track_task(self.final_grace_task)
        else:
            # partial – si tenemos pending_final activo, lo anexamos durante la ventana
            if self.pending_final:
                self.pending_final += " " + transcript.strip()

    async def _commit_final_after_grace(self):
        try:
            await asyncio.sleep(self.grace_ms)
        except asyncio.CancelledError:
            return
        if not self.pending_final:
            return
        final_text = self.pending_final
        self.pending_final = None
        logger.debug("🟢 consolidado → %s", final_text)
        # envía a GPT
        self._track_task(asyncio.create_task(self._process_gpt_response(final_text)))

    # ────────────────────────────────────────────────────────────
    # 🤖  GPT ROUND TRIP
    # ────────────────────────────────────────────────────────────
    async def _process_gpt_response(self, user_text: str):
        if self.call_ended or not self.websocket or self.websocket.client_state != WebSocketState.CONNECTED:
            return
        user_msg = {"role": "user", "content": f"[ES] {user_text}"}
        if not self.conversation_history or self.conversation_history[-1] != user_msg:
            self.conversation_history.append(user_msg)
        response = await generate_openai_response_main(generate_openai_prompt(self.conversation_history), model="gpt-4.1-mini")
        if response == "__END_CALL__":
            await self._shutdown(); return
        self.conversation_history.append({"role": "assistant", "content": response})
        logger.info("🤖 IA: %s", response)
        # detección sencilla de modo teléfono
        if any(p in response.lower() for p in ("número de whatsapp", "número de teléfono", "compartir el número")):
            asyncio.create_task(self._activate_accumulating_mode_after_audio())
        await self._play_audio_bytes(text_to_speech(response))
        # si despedida...
        if GOODBYE_PHRASE.lower() in response.lower():
            await self._shutdown()
        else:
            await asyncio.sleep(0.2)
            await self._send_silence_chunk()

    # ────────────────────────────────────────────────────────────
    # ☎️  MODO TELÉFONO
    # ────────────────────────────────────────────────────────────
    def _activate_accumulating_mode(self):
        if self.accumulating_mode:
            return
        logger.info("📞 Modo teléfono ON")
        self.accumulating_mode = True
        self.accumulated_transcripts = []
        self._restart_accumulating_timer()

    async def _activate_accumulating_mode_after_audio(self):
        # espera a terminar de hablar
        while self.is_speaking and not self.call_ended:
            await asyncio.sleep(0.1)
        self._activate_accumulating_mode()

    def _restart_accumulating_timer(self):
        if self.accumulating_timer_task and not self.accumulating_timer_task.done():
            self.accumulating_timer_task.cancel()
        self.accumulating_timer_task = asyncio.create_task(self._accumulating_timer())
        self._track_task(self.accumulating_timer_task)

    async def _accumulating_timer(self):
        try:
            await asyncio.sleep(self.accumulating_timeout_phone)
        except asyncio.CancelledError:
            return
        if not self.accumulating_mode:
            return
        raw = " ".join(self.accumulated_transcripts).strip()
        self.accumulating_mode = False
        self.grace_ms = 0.7
        self._track_task(asyncio.create_task(self._process_gpt_response(raw)))

    # ────────────────────────────────────────────────────────────
    # ⏱️  WATCHDOG DE TIEMPO / SILENCIO + HEARTBEAT
    # ────────────────────────────────────────────────────────────
    async def _monitor_call_timeout(self):
        while not self.call_ended:
            await asyncio.sleep(5)
            now = self._now()
            # heartbeat
            try:
                if self.websocket and self.websocket.client_state == WebSocketState.CONNECTED:
                    await self.websocket.send_json({"event": "heartbeat"})
            except Exception:
                pass
            if now - self.stream_start_time >= CALL_MAX_DURATION:
                logger.info("⏰ Duración máxima excedida")
                break
            if now - self.last_final_ts >= CALL_SILENCE_TIMEOUT:
                logger.info("🛑 Silencio prolongado")
                break
        await self._shutdown()

    # ────────────────────────────────────────────────────────────
    # 🔻  SHUTDOWN LIMPIO
    # ────────────────────────────────────────────────────────────
    async def _shutdown(self):
        if self.call_ended:
            return
        self.call_ended = True
        logger.info("🔻 Cuelga llamada")
        # despedida forzada si no se envió
        try:
            if self.websocket and self.websocket.client_state == WebSocketState.CONNECTED:
                if not any(GOODBYE_PHRASE.lower() in m["content"].lower() for m in self.conversation_history if m["role"] == "assistant"):
                    await self._play_audio_bytes(text_to_speech(GOODBYE_PHRASE))
        except Exception:
            pass
        # cierra STT y WS
        if self.stt_streamer:
            await self.stt_streamer.close()
        if self.websocket and self.websocket.client_state == WebSocketState.CONNECTED:
            await self.websocket.close()
        # cancela tasks
        for t in list(self.active_tasks):
            t.cancel()
        self._reset_all_state()

    # ────────────────────────────────────────────────────────────
    # HELPERS
    # ────────────────────────────────────────────────────────────
    def _track_task(self, task: asyncio.Task):
        self.active_tasks.add(task)
        task.add_done_callback(lambda t: self.active_tasks.discard(t))

# ────────────────────────────────────────────────────────────────
# UTILIDAD PARA CAMBIAR NIVEL DEBUG DESDE main.py
# ────────────────────────────────────────────────────────────────

def set_debug(active: bool = True) -> None:
    level = logging.DEBUG if active else logging.INFO
    for name in ("tw_utils", "aiagent", "buscarslot"):
        logging.getLogger(name).setLevel(level)
