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

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

CALL_MAX_DURATION    = 600
CALL_SILENCE_TIMEOUT = 30
GOODBYE              = "Fue un placer atenderle. Que tenga un excelente día. ¡Hasta luego!"

class TwilioWebSocketManager:
    def __init__(self):
        self.grace_ms = 0.7
        self.phone_timeout = 3.5
        self._reset_state()

    def _now(self): return time.perf_counter()

    def _reset_state(self):
        self.call_ended = False
        self.conversation = []
        self.pending_final = None
        self.acc_mode = False
        self.acc_finals = []
        self.acc_timer_task = None
        self.prev_grace = self.grace_ms
        self.speaking = False
        self.stt = None
        self.websocket = None
        self.stream_sid = None
        self.dg_last_final = self._now()
        self.stream_start_time = self._now()
        self.reactivate_stt_task = None
        self.active_tasks = set()
        self.speaking_lock = asyncio.Lock()

    async def handle_twilio_websocket(self, ws: WebSocket):
        self.websocket = ws
        await ws.accept()
        self._reset_state()
        logger.info("📞 llamada iniciada")

        await asyncio.to_thread(load_free_slots_to_cache, 90)
        await asyncio.to_thread(load_consultorio_data_to_cache)

        try:
            await asyncio.wait_for(self._start_stt(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.error("Timeout iniciando Deepgram")
            await self._shutdown()
            return

        asyncio.create_task(self._watchdog())

        try:
            while True:
                data = json.loads(await ws.receive_text())
                evt = data.get("event")
                if evt == "start":
                    self.stream_sid = data["streamSid"]
                    await self._play_tts(self._greeting())
                elif evt == "media":
                    payload = base64.b64decode(data["media"]["payload"])
                    if not self.speaking and self.stt:
                        await self.stt.send_audio(payload)
                    else:
                        logger.warning("⚠️ Audio ignorado: STT no iniciado o hablando")
                elif evt == "stop":
                    break
        finally:
            await self._shutdown()

    async def _start_stt(self):
        self.stt = DeepgramSTTStreamer(self._dg_cb)
        await self.stt.start_streaming()

    def _greeting(self):
        h = get_cancun_time().hour
        if 3 <= h < 12: return "¡Buenos días! Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"
        if h >= 20 or h < 3: return "¡Buenas noches! Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"
        return "¡Buenas tardes! Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"

    def _dg_cb(self, txt: str, final: bool):
        if not txt.strip() or self.call_ended:
            return
        self.dg_last_final = self._now()

        if self.acc_mode:
            if final:
                self.acc_finals.append(txt.strip())
                self._restart_acc_timer()
            return

        if final:
            self.pending_final = txt.strip()
            self._restart_acc_timer()
            self._track_task(asyncio.create_task(self._commit_final()))

    async def _commit_final(self):
        await asyncio.sleep(self.grace_ms)
        if not self.pending_final:
            return
        text = self.pending_final
        self.pending_final = None
        await self._send_to_gpt(text)

    async def _enter_phone_mode(self):
        if self.acc_mode:
            return
        logger.info("📞 modo teléfono ON")
        self.acc_mode, self.prev_grace = True, self.grace_ms
        self.grace_ms = 3.5
        if self.pending_final:
            self.acc_finals.append(self.pending_final.strip())
            self.pending_final = None
        self._restart_acc_timer()

    def _restart_acc_timer(self):
        if self.acc_timer_task and not self.acc_timer_task.done():
            self.acc_timer_task.cancel()
        self.acc_timer_task = asyncio.create_task(self._acc_timer())
        self._track_task(self.acc_timer_task)

    async def _acc_timer(self):
        await asyncio.sleep(self.phone_timeout)
        if not self.acc_mode:
            return
        full = " ".join(self.acc_finals).strip()
        self.acc_mode = False
        await self._send_to_gpt(full)

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

        if any(k in resp.lower() for k in ("número de whatsapp", "número de teléfono", "compartir el número")):
            asyncio.create_task(self._enter_phone_mode())
        if "¿cuál es el motivo" in resp.lower():
            self.acc_mode = False
            self.grace_ms = self.prev_grace

        await self._play_tts(resp)

    async def _play_tts(self, text: str):
        if not self.websocket:
            return
        try:
            audio = text_to_speech(text)
        except Exception as e:
            logger.error("Error TTS: %s", str(e))
            return
        if not audio:
            logger.error("TTS vacío para: %s", text)
            return

        duration = len(audio) / 8000
        async with self.speaking_lock:
            self.speaking = True
        if self.stt:
            delay = max(0, duration - 0.5)
            self.reactivate_stt_task = asyncio.create_task(self._reactivate_stt_after(delay))
            self._track_task(self.reactivate_stt_task)

        chunk, off = 512, 0
        while off < len(audio) and not self.call_ended:
            await self.websocket.send_text(json.dumps({
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": base64.b64encode(audio[off:off+chunk]).decode()}
            }))
            off += chunk
            await asyncio.sleep(chunk / 8000)

        async with self.speaking_lock:
            self.speaking = False

    async def _reactivate_stt_after(self, delay: float):
        await asyncio.sleep(delay)
        async with self.speaking_lock:
            self.speaking = False

    async def _watchdog(self):
        while not self.call_ended:
            await asyncio.sleep(20)
            if self.websocket and self.websocket.client_state == WebSocketState.CONNECTED:
                await self.websocket.send_json({"event": "heartbeat"})
            now = self._now()
            if now - self.dg_last_final > CALL_SILENCE_TIMEOUT:
                break
            if now - self.stream_start_time > CALL_MAX_DURATION:
                break
        await self._shutdown()

    def _track_task(self, task):
        self.active_tasks.add(task)
        task.add_done_callback(lambda t: self.active_tasks.discard(t))

    async def _shutdown(self):
        if self.call_ended:
            return
        self.call_ended = True

        try:
            if self.websocket and self.websocket.client_state == WebSocketState.CONNECTED:
                if not any(GOODBYE.lower() in m["content"].lower() for m in self.conversation if m["role"] == "assistant"):
                    await self._play_tts(GOODBYE)
        except Exception:
            pass

        if self.stt:
            await self.stt.close()
        if self.websocket and self.websocket.client_state == WebSocketState.CONNECTED:
            await self.websocket.close()

        for task in list(self.active_tasks):
            task.cancel()
        self._reset_state()

def set_debug(active: bool = True) -> None:
    level = logging.DEBUG if active else logging.INFO
    for name in ("tw_utils", "aiagent", "buscarslot"):
        logging.getLogger(name).setLevel(level)
