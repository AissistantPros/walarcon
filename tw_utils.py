# tw_utils.py
"""
WebSocket manager para Twilio <-> Deepgram <-> GPT‑4o‑mini
----------------------------------------------------------------
Cambios 26‑abr‑2025
• Nivel de *logging* ⇢ DEBUG global (cambiar en main.py si se desea menos ruido).
• Métricas de latencia: Deepgram, ventana de gracia, OpenAI, ElevenLabs, envío de audio.
• Logs detallados de finales acumulados (🟡 añadido, 🟢 consolidado).
• Captura segura de número telefónico con reintentos controlados.
• Colgado automático por silencio o tiempo máximo.
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

# ────────────────────────────────────────────────────────────────
# CONFIG LOGGING
# ────────────────────────────────────────────────────────────────

def set_debug(active: bool = True) -> None:
    """Activa o desactiva los logs DEBUG de nuestros módulos internos."""
    level = logging.DEBUG if active else logging.INFO
    for name in ("tw_utils", "aiagent", "buscarslot"):
        logging.getLogger(name).setLevel(level)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# ────────────────────────────────────────────────────────────────
# CONSTANTES GLOBALES
# ────────────────────────────────────────────────────────────────
CURRENT_CALL_MANAGER: Optional["TwilioWebSocketManager"] = None
CALL_MAX_DURATION = 600            # 10 min
CALL_SILENCE_TIMEOUT = 30          # 30 s sin finales de STT
GRACE_MS_NORMAL = 0.7            # Tiempo de gracia estándar
GRACE_MS_PHONE = 3.5             # Tiempo de gracia en modo teléfono
GOODBYE_PHRASE = "Fue un placer atenderle. ¡Hasta luego!"

# =====================================================================
# CLASE PRINCIPAL
# =====================================================================
class TwilioWebSocketManager:
    """Gestiona la sesión RTC completa con Twilio, Deepgram, GPT‑4o y ElevenLabs."""

    # ────────────────────────────────────────────────────────────
    # 🚧 CONSTRUCTOR & RESET
    # ────────────────────────────────────────────────────────────
    def __init__(self) -> None:
        self.grace_ms = GRACE_MS_NORMAL                      # ventana de gracia para pegar partials
        self.phone_attempts = 0                   # reintentos de número
        self.accumulating_timer_task = None
        self.final_grace_task = None
        self._reset_all_state()

    def _now(self) -> float:
        return time.perf_counter()

    def _reset_all_state(self):
        logger.debug("🧼 Reset interno completo")
        # estado conversación
        self.call_ended = False
        self.conversation_history = []
        self.pending_final: Optional[str] = None
        # modo teléfono
        self.accumulating_mode = False
        self.accumulated_transcripts = []
        self.phone_attempts = 0
        self._cancel_accumulating_timer()
        # runtime refs
        self.current_gpt_task = None
        self.stt_streamer = None
        self.is_speaking = False
        self.stream_sid = None
        self.websocket = getattr(self, "websocket", None)
        self.speaking_lock = asyncio.Lock()
        # tiempos
        now = self._now()
        self.stream_start_time = now
        self.last_final_ts = now
        self._dg_prev_final_ts = now
        self._dg_first_final_ts = None
        self._dg_final_started_ts = None

    # ────────────────────────────────────────────────────────────
    # 📞  MANEJO WEBSOCKET TWILIO
    # ────────────────────────────────────────────────────────────
    async def handle_twilio_websocket(self, websocket: WebSocket):
        """Punto de entrada principal: atiende la conexión de Twilio."""
        self.websocket = websocket
        await websocket.accept()
        self._reset_all_state()
        global CURRENT_CALL_MANAGER; CURRENT_CALL_MANAGER = self
        logger.info("📞 Llamada iniciada")

        # precarga datos de negocio
        try:
            load_free_slots_to_cache(90)
            load_consultorio_data_to_cache()
        except Exception as e:
            logger.warning("⚠️ Precarga falló: %s", e, exc_info=True)

        # inicializar Deepgram
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


    async def _reactivate_stt_after(self, delay: float):
        """Reactiva el STT después del tiempo indicado, siempre que el streamer siga activo."""
        await asyncio.sleep(delay)
        if self.stt_streamer and not self.call_ended:
            await self._send_silence_chunk()  # envía un pequeño chunk para despertar STT



    # ────────────────────────────────────────────────────────────
    # 🔊  AUDIO & SALUDO
    # ────────────────────────────────────────────────────────────
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

    # ────────────────────────────────────────────────────────────
    # 🎙️  CALLBACK DEEPGRAM
    # ────────────────────────────────────────────────────────────
    def _stt_callback(self, transcript: str, is_final: bool):
        """Callback principal que Deepgram ejecuta cada vez que hay una transcripción."""
        if not transcript:
            return

        now = self._now()

        # Métricas de latencia
        if is_final and self._dg_first_final_ts is None:
            self._dg_first_final_ts = now
            logger.debug("⏱️ Deepgram 1ª final tras %.0f ms", (now - self.stream_start_time) * 1000)

        if is_final:
            logger.debug("⏱️ Deepgram Δ final %.0f ms", (now - self._dg_prev_final_ts) * 1000)
            self._dg_prev_final_ts = now

        # Acumulación parcial mientras esperamos el grace
        if not is_final and self.pending_final:
            if self.final_grace_task and not self.final_grace_task.done():
                self.final_grace_task.cancel()
            new = transcript.strip()
            if new not in self.pending_final:
                self.pending_final += " " + new
            return

        # Armar nuevo pending_final
        if is_final:
            self.pending_final = (self.pending_final + " " if self.pending_final else "") + transcript.strip()
            self._dg_final_started_ts = now

            # Reiniciar temporizador de gracia según modo actual
            if self.final_grace_task and not self.final_grace_task.done():
                self.final_grace_task.cancel()
            self.final_grace_task = asyncio.create_task(self._commit_final_after_grace())








    async def _commit_final_after_grace(self):
        try:
            await asyncio.sleep(self.grace_ms)
        except asyncio.CancelledError:
            return

        final_text = self.pending_final or ""
        self.pending_final = None
        self.last_final_ts = self._now()
        logger.debug("🟢 consolidado → %s", final_text)

        # SIEMPRE acumula
        self._accumulate_transcript(final_text)

        # Si hay tarea anterior de GPT, cancélala
        if self.current_gpt_task and not self.current_gpt_task.done():
            self.current_gpt_task.cancel()

        self.current_gpt_task = asyncio.create_task(self.process_gpt_response(final_text))


    # ────────────────────────────────────────────────────────────
    # ☎️  MODO TELÉFONO
    # ────────────────────────────────────────────────────────────
    def _activate_phone_mode(self):
        """
        Activa el modo teléfono:
        - Cambia grace_ms a 3.5 segundos.
        - Marca que estamos en acumulación extendida.
        """
        if self.accumulating_mode:
            return
        logger.info("📞 Modo teléfono ON")
        self.accumulating_mode = True
        self.grace_ms = GRACE_MS_PHONE

    def _exit_phone_mode(self):
        """
        Desactiva el modo teléfono:
        - Regresa grace_ms a 0.7 segundos.
        - Mantiene la acumulación normal.
        """
        if not self.accumulating_mode:
            return
        logger.info("📞 Modo teléfono OFF")
        self.accumulating_mode = False
        self.grace_ms = GRACE_MS_NORMAL




    def _accumulate_transcript(self, fragment: str):
        """Añade un fragmento al buffer y reinicia el temporizador de acumulación."""
        self.accumulated_transcripts.append(fragment.strip())
        self._start_accumulating_timer(reset=True)


    def _start_accumulating_timer(self, reset=False):
        """Arranca o reinicia el temporizador de acumulación."""
        if reset:
            self._cancel_accumulating_timer()
        loop = asyncio.get_event_loop()
        self.accumulating_timer_task = loop.create_task(self._accumulating_timer(self.grace_ms))


    def _cancel_accumulating_timer(self):
        if self.accumulating_timer_task and not self.accumulating_timer_task.done():
            self.accumulating_timer_task.cancel()
        self.accumulating_timer_task = None

    async def _accumulating_timer(self, timeout):
        """Espera el tiempo de gracia y luego hace flush del texto acumulado."""
        try:
            await asyncio.sleep(timeout)
            self._flush_accumulated_transcripts()
        except asyncio.CancelledError:
            pass


    def _flush_accumulated_transcripts(self):
        """Combina todo lo acumulado, lo limpia y lo manda a procesar."""
        self._cancel_accumulating_timer()
        raw = " ".join(self.accumulated_transcripts).strip()
        self.accumulated_transcripts = []

        if not raw:
            return

        if self.call_ended:
            return

        asyncio.create_task(self.process_gpt_response(raw))





    async def _confirm_or_retry_phone(self, texto_usuario: str):
        digits = re.sub(r"\D", "", texto_usuario)
        if len(digits) == 10:
            fmt = ", ".join([digits[i:i + 2] for i in range(0, 10, 2)])
            await self._play_audio_bytes(text_to_speech(f"¿Es correcto el número {fmt}?"))
            self.accumulating_mode = False
            return

        self.phone_attempts += 1
        if self.phone_attempts >= 3:
            await self._play_audio_bytes(text_to_speech(
                "No logré entender el número. ¿Podría enviarlo por WhatsApp al nueve, nueve, ocho, dos, trece, setenta y cuatro, setenta y siete, por favor?"
            ))
            self.accumulating_mode = False
            return

        prompts = [
            "¿Podría repetir el número sin pausas, por favor?",
            "Intente decirlo sin pausas y en pares, por favor"
        ]
        await self._play_audio_bytes(text_to_speech(prompts[self.phone_attempts - 1]))
        await asyncio.sleep(0.5)
        self._start_accumulating_timer(reset=True)


        
    async def _activate_phone_mode_after_audio(self):
        while self.is_speaking and not self.call_ended:
            await asyncio.sleep(0.1)
        self._activate_phone_mode()


    # ────────────────────────────────────────────────────────────
    # 🤖  GPT ROUND TRIP
    # ────────────────────────────────────────────────────────────
    async def process_gpt_response(self, user_text: str):
        """
        • Envía el texto del usuario a GPT (vía aiagent).
        • Maneja triggers de modo teléfono.
        • Gestiona fin de llamada con despedida obligatoria.
        """
        if self.call_ended or not self.websocket or self.websocket.client_state != WebSocketState.CONNECTED:
            return

        # Guarda turno del usuario
        self.conversation_history.append({"role": "user", "content": f"[ES] {user_text}"})

        # Llama a GPT
        reply = await generate_openai_response_main(
            generate_openai_prompt(self.conversation_history),
            model="gpt-4.1-mini"
        )

        # ── Protocolo completo para end_call ──────────────────────
        if reply.strip() == "__END_CALL__":
            logger.info("🚪 Protocolo de cierre activado por IA")

            despedida_ya_dicha = any(
                any(k in m["content"].lower() for k in ("gracias", "hasta luego", "placer atenderle", "excelente día"))
                for m in self.conversation_history
                if m["role"] == "assistant"
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

        # Guarda respuesta normal
        self.conversation_history.append({"role": "assistant", "content": reply})
        logger.info("🤖 IA: %s", reply)

        # ── Triggers de modo teléfono ─────────────────────────────
        if any(k in reply.lower() for k in (
            "número de whatsapp",
            "número de teléfono",
            "compartir el número",
            "me puede compartir el número de whatsapp para enviarle la confirmación"
        )):
            asyncio.create_task(self._activate_phone_mode_after_audio())

        # Salida de modo teléfono (cuando pregunta por el motivo)
        if "cuál es el motivo de la consulta" in reply.lower():
            self._exit_phone_mode()

        # Pronuncia la respuesta
        await self._play_audio_bytes(text_to_speech(reply))

        # Empuja un pequeño chunk de silencio y continúa
        await asyncio.sleep(0.2)
        await self._send_silence_chunk()




    # ────────────────────────────────────────────────────────────
    # 📤  ENVÍO AUDIO A TWILIO
    # ────────────────────────────────────────────────────────────
    async def _play_audio_bytes(self, audio_data: bytes):
        """
        Envía el audio (μ‑law 8 kHz) a Twilio en chunks de 512 bytes.
        • Mientras habla TTS, self.is_speaking = True.
        • Reactiva el STT 1 s antes de que termine para no perder la primera palabra del usuario.
        """
        if not audio_data or not self.websocket or self.websocket.client_state != WebSocketState.CONNECTED:
            return

        duration = len(audio_data) / 8000.0  # segundos que dura el audio
        async with self.speaking_lock:
            self.is_speaking = True

        # ── Reactivar STT un segundo antes ───────────────────────────
        if self.stt_streamer:
            delay = max(0.0, duration - 1.0)
            asyncio.create_task(self._reactivate_stt_after(delay))

        # ── Stream por chunks a Twilio ───────────────────────────────
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
            await asyncio.sleep(chunk_size / 8000.0)  # ritmo realtime

        async with self.speaking_lock:
            self.is_speaking = False

    # ────────────────────────────────────────────────────────────
    # ⏱️  WATCHDOG DE TIEMPO / SILENCIO
    # ────────────────────────────────────────────────────────────
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

    # ────────────────────────────────────────────────────────────
    # 🔻  SHUTDOWN LIMPIO
    # ────────────────────────────────────────────────────────────
    async def _shutdown(self):
        if self.call_ended:
            return
        self.call_ended = True
        logger.info("🔻 Cuelga llamada")


        if self.stt_streamer:
            await self.stt_streamer.close()
        if self.websocket and self.websocket.client_state == WebSocketState.CONNECTED:
            await self.websocket.close()
        self._reset_all_state()