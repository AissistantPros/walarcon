# tw_utils.py  (versión simplificada)
import asyncio, base64, json, logging, re, time
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

# ───────── LOGGING ─────────
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# ───────── CONSTANTES ──────
CALL_MAX_DURATION   = 600
CALL_SILENCE_TIMEOUT = 30
GOODBYE = "Fue un placer atenderle. Que tenga un excelente día. ¡Hasta luego!"

class TwilioWebSocketManager:
    # ───────── CONSTRUCTOR ──────
    def __init__(self):
        self.grace_ms      = 0.7          # ventana normal
        self.phone_timeout = 3.5          # silencio para mandar final acumulado
        self._reset_state()

    # ───────── STATE helpers ─────
    def _now(self): return time.perf_counter()

    def _reset_state(self):
        self.call_ended      = False
        self.conversation    = []
        self.pending_final   = None       # texto aún no enviado a GPT
        self.dg_last_final   = self._now()
        self.acc_mode        = False      # estamos capturando teléfono
        self.acc_finals      = []         # finals Deepgram acumulados
        self.acc_timer_task  = None
        self.prev_grace      = 0.7        # para restaurar después
        # refs runtime
        self.stt, self.ws, self.stream_sid = None, None, None
        self.speaking = False

    # ───────── WEBSOCKET entry ───
    async def handle_twilio_websocket(self, ws: WebSocket):
        self.ws = ws
        await ws.accept()
        self._reset_state()
        logger.info("📞 llamada iniciada")

        # precarga negocio
        load_free_slots_to_cache(90); load_consultorio_data_to_cache()

        # Deepgram
        self.stt = DeepgramSTTStreamer(self._dg_cb); await self.stt.start_streaming()
        asyncio.create_task(self._watchdog())

        try:
            while True:
                data = json.loads(await ws.receive_text())
                evt  = data.get("event")
                if evt == "start":
                    self.stream_sid = data["streamSid"]
                    await self._play_tts(self._greeting())
                elif evt == "media":
                    if not self.speaking:
                        await self.stt.send_audio(base64.b64decode(data["media"]["payload"]))
                elif evt == "stop":
                    break
        finally:
            await self._shutdown()

    # ───────── GREETING ──────────
    def _greeting(self):
        h = get_cancun_time().hour
        if 3 <= h < 12: return "¡Buenos días! Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"
        if h >= 20 or h < 3: return "¡Buenas noches! Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"
        return "¡Buenas tardes! Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"

    # ───────── DEEPGRAM callback ─
    def _dg_cb(self, txt:str, final:bool):
        if not txt.strip() or self.call_ended: return
        self.dg_last_final = self._now()

        # ----- modo teléfono -----
        if self.acc_mode:
            if final:
                self.acc_finals.append(txt.strip())
                self._restart_acc_timer()
            return

        # ----- modo normal --------
        if final:
            self.pending_final = txt.strip()
            asyncio.create_task(self._commit_final())

    async def _commit_final(self):
        await asyncio.sleep(self.grace_ms)
        if not self.pending_final: return
        text = self.pending_final; self.pending_final = None
        await self._send_to_gpt(text)

    # ───────── PHONE MODE helpers ─
    async def _enter_phone_mode(self):
        if self.acc_mode: return
        logger.info("📞 modo teléfono ON")
        self.acc_mode   = True
        self.prev_grace = self.grace_ms
        self.grace_ms   = 3.5          # más paciencia
        self.acc_finals = []
        self._restart_acc_timer()

    def _restart_acc_timer(self):
        if self.acc_timer_task and not self.acc_timer_task.done():
            self.acc_timer_task.cancel()
        self.acc_timer_task = asyncio.create_task(self._acc_timer())

    async def _acc_timer(self):
        await asyncio.sleep(self.phone_timeout)
        if not self.acc_mode: return
        full = " ".join(self.acc_finals).strip()
        self.acc_mode = False
        self.grace_ms = self.prev_grace
        await self._send_to_gpt(full)

    # ───────── GPT round-trip ────
    async def _send_to_gpt(self, user_text:str):
        self.conversation.append({"role":"user","content":f"[ES] {user_text}"})
        resp = await generate_openai_response_main(generate_openai_prompt(self.conversation),
                                                   model="gpt-4.1-mini")
        if resp == "__END_CALL__": await self._shutdown(); return
        self.conversation.append({"role":"assistant","content":resp})
        logger.info("🤖 %s", resp)

        # ¿pasamos al modo teléfono?
        if any(key in resp.lower() for key in
               ["número de whatsapp","número de teléfono","compartir el número"]):
            asyncio.create_task(self._enter_phone_mode())

        # ¿salimos cuando IA pregunte motivo?
        if "¿cuál es el motivo" in resp.lower():
            self.acc_mode, self.grace_ms = False, self.prev_grace

        await self._play_tts(resp)

    # ───────── AUDIO a Twilio ────
    async def _play_tts(self, text:str):
        audio = text_to_speech(text)
        self.speaking = True
        ch, off = 512, 0
        while off < len(audio) and not self.call_ended:
            await self.ws.send_text(json.dumps({
                "event":"media","streamSid":self.stream_sid,
                "media":{"payload":base64.b64encode(audio[off:off+ch]).decode()}
            }))
            off += ch; await asyncio.sleep(ch/8000)
        self.speaking = False

    # ───────── WATCHDOG ──────────
    async def _watchdog(self):
        while not self.call_ended:
            await asyncio.sleep(2)
            if self._now() - self.dg_last_final > CALL_SILENCE_TIMEOUT: break
            if self._now() - self.stream_start_time > CALL_MAX_DURATION: break
        await self._shutdown()

    # ───────── SHUTDOWN ──────────
    async def _shutdown(self):
        if self.call_ended: return
        self.call_ended = True
        if not any(GOODBYE.lower() in m["content"].lower()
                   for m in self.conversation if m["role"]=="assistant"):
            await self._play_tts(GOODBYE)
        if self.stt: await self.stt.close()
        if self.ws and self.ws.client_state==WebSocketState.CONNECTED:
            await self.ws.close()
        self._reset_state()



# ────────────────────────────────────────────────────────────────
# FUNCIÓN set_debug (para main.py)
# ────────────────────────────────────────────────────────────────
def set_debug(active: bool = True) -> None:
    """
    Activa o desactiva los logs DEBUG de los módulos internos.
    Se mantiene por compatibilidad con main.py
    """
    level = logging.DEBUG if active else logging.INFO
    for name in ("tw_utils", "aiagent", "buscarslot"):
        logging.getLogger(name).setLevel(level)
