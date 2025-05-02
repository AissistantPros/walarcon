# tw_utils.py
import asyncio, base64, json, logging, time
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

# ───────────────────────────────────────── LOGGING ──────────────────────────
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# ───────────────────────────────────────── CONSTANTES ────────────────────────
DEFAULT_GRACE_MS   = 0.7      # ventana anti‑finales falsos
PHONE_GRACE_MS     = 3.5      # ventana extendida dictando números
CALL_MAX_DURATION  = 600      # 10 min
CALL_SILENCE_TMO   = 30       # 30 s sin finales
GOODBYE_PHRASE     = "Fue un placer atenderle. Que tenga un excelente día. ¡Hasta luego!"

# ═════════════════════════ CLASS ════════════════════════════════════════════
class TwilioWebSocketManager:
    """Maneja la llamada Twilio ⇆ Deepgram ⇆ GPT ⇆ ElevenLabs."""

    def __init__(self) -> None:
        self.grace_ms = DEFAULT_GRACE_MS
        self.accum_tmo_phone = PHONE_GRACE_MS
        self.active_tasks: Set[asyncio.Task] = set()
        self.speaking_lock = asyncio.Lock()
        self._reset_all_state()

    # util de tiempo
    def _now(self) -> float:
        return time.perf_counter()

    # ─────────────────────────── RESET STATE ────────────────────────────────
    def _reset_all_state(self):
        logger.debug("🧼 Reset interno completo")
        self.call_ended             = False
        self.conversation_history   = []
        self.pending_final: Optional[str] = None
        # modo teléfono
        self.accumulating_mode      = False
        self.accumulated_final_txts = []
        self.acc_timer_task         = None
        self.final_grace_task       = None
        self.reactivate_stt_task    = None
        # objetos
        self.stt_streamer  = None
        self.websocket     = None
        self.stream_sid    = None
        # tiempos
        now = self._now()
        self.stream_start_time = now
        self.last_final_ts     = now
        self.is_speaking       = False
        # mem
        self._prev_grace = DEFAULT_GRACE_MS

    # ───────────────────────── WEBSOCKET HANDLER ────────────────────────────
    async def handle_twilio_websocket(self, ws: WebSocket):
        self.websocket = ws
        await ws.accept()
        self._reset_all_state()
        logger.info("📞 Nueva llamada entrante desde Twilio.")

        # precarga datos negocio
        await asyncio.gather(
            asyncio.to_thread(load_free_slots_to_cache, 90),
            asyncio.to_thread(load_consultorio_data_to_cache),
        )

        self.stt_streamer = DeepgramSTTStreamer(self._stt_callback)
        try:
            await asyncio.wait_for(self.stt_streamer.start_streaming(), 10)
        except asyncio.TimeoutError:
            logger.error("❌ Timeout iniciando Deepgram"); await self._shutdown(); return

        self._track_task(asyncio.create_task(self._monitor_call_timeout()))

        try:
            while True:
                evt_json = json.loads(await ws.receive_text())
                evt      = evt_json.get("event")
                if evt == "start":
                    self.stream_sid = evt_json.get("streamSid")
                    await self._play_audio_bytes(text_to_speech(self._greeting()))
                elif evt == "media":
                    if not self.is_speaking and self.stt_streamer:
                        payload = base64.b64decode(evt_json["media"]["payload"])
                        await self.stt_streamer.send_audio(payload)
                elif evt == "stop":
                    logger.info("🛑 Twilio envió stop"); break
        finally:
            await self._shutdown()

    # ───────────────────────────── AUDIO / SALUDO ───────────────────────────
    def _greeting(self):
        h = get_cancun_time().hour
        if 3 <= h < 12: return "¡Buenos días! Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"
        if h >= 20 or h < 3: return "¡Buenas noches! Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"
        return "¡Buenas tardes! Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"

    async def _play_audio_bytes(self, audio: bytes):
        if not audio or not self.websocket or self.websocket.client_state != WebSocketState.CONNECTED:
            return
        dur = len(audio) / 8000.0
        async with self.speaking_lock: self.is_speaking = True
        if self.stt_streamer:
            delay = max(0.0, dur - 1.0)                   # reactiva STT 1 s antes
            self.reactivate_stt_task = asyncio.create_task(self._reactivate_stt_after(delay))
            self._track_task(self.reactivate_stt_task)

        chunk, off = 512, 0
        while off < len(audio) and not self.call_ended:
            await self.websocket.send_text(json.dumps({
                "event": "media", "streamSid": self.stream_sid,
                "media": {"payload": base64.b64encode(audio[off:off+chunk]).decode()}
            }))
            off += chunk
            await asyncio.sleep(chunk / 8000.0)
        async with self.speaking_lock: self.is_speaking = False

    async def _reactivate_stt_after(self, delay: float):
        await asyncio.sleep(delay)
        async with self.speaking_lock: self.is_speaking = False

    async def _send_silence_chunk(self):
        if self.stt_streamer:
            try: await self.stt_streamer.send_audio(b"\xff" * 320)
            except Exception: pass

    # ───────────────────────── CALLBACK DEEPGRAM ────────────────────────────
    def _stt_callback(self, txt: str, final: bool):
        if not txt or self.call_ended: return
        if final: self.last_final_ts = self._now()

        # Modo teléfono
        if self.accumulating_mode:
            if final:
                self.accumulated_final_txts.append(txt.strip())
                self._restart_acc_timer()
            return

        # Modo normal
        if final:
            self.pending_final = (self.pending_final + " " if self.pending_final else "") + txt.strip()
            if self.final_grace_task and not self.final_grace_task.done():
                self.final_grace_task.cancel()
            self.final_grace_task = asyncio.create_task(self._commit_after_grace())
            self._track_task(self.final_grace_task)
        else:
            if self.pending_final:
                self.pending_final += " " + txt.strip()

    async def _commit_after_grace(self):
        try: await asyncio.sleep(self.grace_ms)
        except asyncio.CancelledError: return
        if not self.pending_final: return
        final_text = self.pending_final; self.pending_final = None
        logger.debug("🟢 consolidado → %s", final_text)
        self._track_task(asyncio.create_task(self._process_gpt_response(final_text)))

    # ───────────────────────── GPT ROUND TRIP ───────────────────────────────
    async def _process_gpt_response(self, user_text: str):
        if self.call_ended or not self.websocket: return
        self.conversation_history.append({"role": "user", "content": f"[ES] {user_text}"})
        reply = await generate_openai_response_main(generate_openai_prompt(self.conversation_history),
                                                    model="gpt-4.1-mini")
        if reply == "__END_CALL__": await self._shutdown(); return
        self.conversation_history.append({"role": "assistant", "content": reply})
        logger.info("🤖 IA: %s", reply)

        # triggers modo teléfono
        if any(k in reply.lower() for k in ("número de whatsapp", "número de teléfono", "compartir el número")):
            asyncio.create_task(self._activate_phone_mode_after_audio())
        if "cual es el motivo de la consulta" in reply.lower():
            self.accumulating_mode = False
            self.grace_ms = DEFAULT_GRACE_MS

        await self._play_audio_bytes(text_to_speech(reply))
        if GOODBYE_PHRASE.lower() in reply.lower(): await self._shutdown()
        else:
            await asyncio.sleep(0.2); await self._send_silence_chunk()

    # ───────────────────────────── MODO TELÉFONO ────────────────────────────
    def _activate_phone_mode(self):
        if self.accumulating_mode: return
        logger.info("📞 Modo teléfono ON")
        self.accumulating_mode = True
        self.accumulated_final_txts = []
        self._prev_grace, self.grace_ms = self.grace_ms, PHONE_GRACE_MS
        self._restart_acc_timer()

    async def _activate_phone_mode_after_audio(self):
        while self.is_speaking and not self.call_ended:
            await asyncio.sleep(0.1)
        self._activate_phone_mode()

    # timers
    def _restart_acc_timer(self):
        if self.acc_timer_task and not self.acc_timer_task.done():
            self.acc_timer_task.cancel()
        self.acc_timer_task = asyncio.create_task(self._accumulating_timer())
        self._track_task(self.acc_timer_task)

    async def _accumulating_timer(self):
        try: await asyncio.sleep(self.accum_tmo_phone)
        except asyncio.CancelledError: return
        if not self.accumulating_mode: return
        full_text = " ".join(self.accumulated_final_txts).strip()
        self.accumulating_mode = False
        self.grace_ms = DEFAULT_GRACE_MS
        self._track_task(asyncio.create_task(self._process_gpt_response(full_text)))

    # ─────────────────── WATCHDOG SILENCIO / DURACIÓN ───────────────────────
    async def _monitor_call_timeout(self):
        while not self.call_ended:
            await asyncio.sleep(5)
            try:
                if self.websocket and self.websocket.client_state == WebSocketState.CONNECTED:
                    await self.websocket.send_json({"event": "heartbeat"})
            except Exception: pass
            now = self._now()
            if now - self.stream_start_time >= CALL_MAX_DURATION: break
            if now - self.last_final_ts     >= CALL_SILENCE_TMO: break
        await self._shutdown()

    # ────────────────────────────── SHUTDOWN ────────────────────────────────
    async def _shutdown(self):
        if self.call_ended: return
        self.call_ended = True
        logger.info("🔻 Cuelga llamada")

        # despedida si no se dijo
        try:
            if self.websocket and self.websocket.client_state == WebSocketState.CONNECTED:
                if not any(GOODBYE_PHRASE.lower() in m["content"].lower()
                           for m in self.conversation_history if m["role"] == "assistant"):
                    await self._play_audio_bytes(text_to_speech(GOODBYE_PHRASE))
        except Exception: pass

        if self.stt_streamer: await self.stt_streamer.close()
        if self.websocket and self.websocket.client_state == WebSocketState.CONNECTED:
            await self.websocket.close()

        for t in list(self.active_tasks): t.cancel()
        self._reset_all_state()

    # ───────────────────────────── HELPERS ──────────────────────────────────
    def _track_task(self, task: asyncio.Task):
        self.active_tasks.add(task)
        task.add_done_callback(lambda t: self.active_tasks.discard(t))

# utilidad para main.py
def set_debug(active: bool = True):
    lvl = logging.DEBUG if active else logging.INFO
    for name in ("tw_utils", "aiagent", "buscarslot"):
        logging.getLogger(name).setLevel(lvl)
