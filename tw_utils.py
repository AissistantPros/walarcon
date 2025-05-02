# tw_utils.py  ← versión simplificada pero estable
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

# ───────── LOGGING ─────────────────────────────────────────────
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# ───────── CONSTANTES ─────────────────────────────────────────
CALL_MAX_DURATION    = 600   # seg
CALL_SILENCE_TIMEOUT = 30    # seg sin finals
GOODBYE              = "Fue un placer atenderle. Que tenga un excelente día. ¡Hasta luego!"

# ───────────────────────────────────────────────────────────────
class TwilioWebSocketManager:
    def __init__(self):
        self.grace_ms      = 0.7   # ventana normal
        self.phone_timeout = 3.5   # silencio para enviar final acumul.
        self._reset_state()

    # ───────── helpers de tiempo/estado ────────────────────────
    def _now(self): return time.perf_counter()

    def _reset_state(self):
        self.call_ended     = False
        self.conversation   = []
        self.pending_final  = None
        self.acc_mode       = False      # modo teléfono
        self.acc_finals     = []
        self.acc_timer_task = None
        self.prev_grace     = self.grace_ms
        self.speaking       = False
        self.stt            = None
        self.websocket      = None       # ← siempre presente
        self.stream_sid     = None
        self.dg_last_final  = self._now()
        self.stream_start_time = self._now()   # ← faltaba

    # ───────── entrada WebSocket ───────────────────────────────
    async def handle_twilio_websocket(self, websocket: WebSocket):
        self.websocket = websocket
        await websocket.accept()
        self._reset_state()
        logger.info("📞 llamada iniciada")

        # precarga negocio
        load_free_slots_to_cache(90)
        load_consultorio_data_to_cache()

        # Deepgram
        self.stt = DeepgramSTTStreamer(self._dg_cb)
        await self.stt.start_streaming()

        asyncio.create_task(self._watchdog())

        try:
            while True:
                raw = await websocket.receive_text()
                data = json.loads(raw)
                event = data.get("event")

                if event == "start":
                    self.stream_sid = data["streamSid"]
                    await self._play_tts(self._greeting())
                elif event == "media" and not self.speaking:
                    payload = base64.b64decode(data["media"]["payload"])
                    await self.stt.send_audio(payload)
                elif event == "stop":
                    break
        finally:
            await self._shutdown()

    # ───────── frases de saludo ────────────────────────────────
    def _greeting(self):
        h = get_cancun_time().hour
        if 3 <= h < 12:  return "¡Buenos días! Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"
        if h >= 20 or h < 3:  return "¡Buenas noches! Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"
        return "¡Buenas tardes! Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"

    # ───────── CALLBACK Deepgram ───────────────────────────────
    def _dg_cb(self, txt: str, final: bool):
        if not txt.strip() or self.call_ended:
            return
        self.dg_last_final = self._now()

        # — modo teléfono —
        if self.acc_mode:
            if final:
                self.acc_finals.append(txt.strip())
                self._restart_acc_timer()
            return

        # — modo normal —
        if final:
            self.pending_final = txt.strip()
            asyncio.create_task(self._commit_final())

    async def _commit_final(self):
        await asyncio.sleep(self.grace_ms)
        if not self.pending_final:
            return
        text = self.pending_final
        self.pending_final = None
        await self._send_to_gpt(text)

    # ───────── helpers modo teléfono ───────────────────────────
    async def _enter_phone_mode(self):
        if self.acc_mode:
            return
        logger.info("📞 modo teléfono ON")
        self.acc_mode, self.prev_grace = True, self.grace_ms
        self.grace_ms   = 3.5
        self.acc_finals = []
        self._restart_acc_timer()

    def _restart_acc_timer(self):
        if self.acc_timer_task and not self.acc_timer_task.done():
            self.acc_timer_task.cancel()
        self.acc_timer_task = asyncio.create_task(self._acc_timer())

    async def _acc_timer(self):
        await asyncio.sleep(self.phone_timeout)
        if not self.acc_mode:
            return
        full = " ".join(self.acc_finals).strip()
        self.acc_mode, self.grace_ms = False, self.prev_grace
        await self._send_to_gpt(full)

    # ───────── GPT round-trip ─────────────────────────────────
    async def _send_to_gpt(self, user_text: str):
        self.conversation.append({"role": "user", "content": f"[ES] {user_text}"})
        resp = await generate_openai_response_main(
            generate_openai_prompt(self.conversation), model="gpt-4.1-mini"
        )
        if resp == "__END_CALL__":
            await self._shutdown()
            return

        self.conversation.append({"role": "assistant", "content": resp})
        logger.info("🤖 %s", resp)

        # trigger modo teléfono
        if any(k in resp.lower() for k in ("número de whatsapp", "número de teléfono", "compartir el número")):
            asyncio.create_task(self._enter_phone_mode())
        # salir cuando IA pregunte motivo
        if "¿cuál es el motivo" in resp.lower():
            self.acc_mode, self.grace_ms = False, self.prev_grace

        await self._play_tts(resp)

    # ───────── enviar audio a Twilio ──────────────────────────
    async def _play_tts(self, text: str):
        if not self.websocket:
            return
        audio = text_to_speech(text)
        self.speaking = True
        chunk, off = 512, 0
        while off < len(audio) and not self.call_ended:
            await self.websocket.send_text(json.dumps({
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": base64.b64encode(audio[off:off+chunk]).decode()}
            }))
            off += chunk
            await asyncio.sleep(chunk / 8000)
        self.speaking = False

    # ───────── watchdog (tiempo / silencio) ───────────────────
    async def _watchdog(self):
        while not self.call_ended:
            await asyncio.sleep(2)
            now = self._now()
            if now - self.dg_last_final > CALL_SILENCE_TIMEOUT:
                break
            if now - self.stream_start_time > CALL_MAX_DURATION:
                break
        await self._shutdown()

    # ───────── cierre limpio ─────────────────────────────────
    async def _shutdown(self):
        if self.call_ended:
            return
        self.call_ended = True

        try:
            if self.websocket and self.websocket.client_state == WebSocketState.CONNECTED:
                # despedida si falta
                if not any(GOODBYE.lower() in m["content"].lower() for m in self.conversation if m["role"] == "assistant"):
                    await self._play_tts(GOODBYE)
        except Exception:
            pass  # el cliente puede haberse desconectado

        if self.stt:
            await self.stt.close()
        if self.websocket and self.websocket.client_state == WebSocketState.CONNECTED:
            await self.websocket.close()

        self._reset_state()

# ───────── función set_debug (para main.py) ──────────────────
def set_debug(active: bool = True) -> None:
    level = logging.DEBUG if active else logging.INFO
    for name in ("tw_utils", "aiagent", "buscarslot"):
        logging.getLogger(name).setLevel(level)
