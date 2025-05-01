# tw_utils.py
"""
WebSocket manager para Twilio <-> Deepgram <-> GPT‑4o‑mini
----------------------------------------------------------------
Cambios 26‑abr‑2025
• Nivel de *logging* ⇢ DEBUG global (cambiar en main.py si se desea menos ruido).
• Métricas de latencia: Deepgram, ventana de gracia, OpenAI, ElevenLabs, envío de audio.
• Logs detallados de finales acumulados (🟡 añadido, 🟢 consolidado).
• Se elimina `accumulating_timeout_general` (ya no se usaba).
• Código formateado y comentarios depurados.
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
import re

# --- utilitario de verbosidad ----------------------------------------
def set_debug(active: bool = True) -> None:
    """
    Activa o desactiva los logs DEBUG de nuestros módulos sin
    mostrar la verbosidad de librerías externas.
    """
    level = logging.DEBUG if active else logging.INFO
    for name in ("tw_utils", "aiagent", "buscarslot"):
        logging.getLogger(name).setLevel(level)


logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)  # ← sube el nivel para ver todo

CURRENT_CALL_MANAGER: Optional["TwilioWebSocketManager"] = None

CALL_MAX_DURATION = 600      # 10 min
CALL_SILENCE_TIMEOUT = 30    # 30 s sin finals → colgar
GOODBYE_PHRASE = (
    "Fue un placer atenderle. Que tenga un excelente día. ¡Hasta luego!"
)


class TwilioWebSocketManager:
    """Gestiona toda la llamada RTC"""

    def __init__(self) -> None:
       # margen de gracia para pegar finals de Deepgram (seg)
        self.grace_ms = 0.7
        # pausa máxima SIN audio entre finales para flush (teléfono)
        self.accumulating_timeout_phone = 3.5
        # contador de intentos de dictado
        self.phone_attempts = 0

        # punteros / tareas
        self.accumulating_timer_task = None
        self.final_grace_task = None
        self._reset_all_state()

    # ------------------------------------------------------------------
    # tools
    # ------------------------------------------------------------------
    def _now(self) -> float:
        """High‑resolution timestamp (seg)."""
        return time.perf_counter()

    # ------------------------------------------------------------------
    def _reset_all_state(self):
        logger.debug("🧼 Reset interno completo")
        self.call_ended = False
        self.conversation_history = []
        self.expecting_number = False
        self.pending_final: Optional[str] = None
        self.accumulating_mode = False
        self.accumulated_transcripts = []
        self._cancel_accumulating_timer()

        # referencias runtime
        self.current_gpt_task = None
        self.stt_streamer = None
        self.is_speaking = False
        self.stream_sid = None
        self.websocket = getattr(self, "websocket", None)

        # tiempos
        now = self._now()
        self.stream_start_time = now
        self.last_partial_ts = now
        self.last_final_ts = now
        self._dg_prev_final_ts = now      # latencia entre finals
        self._dg_first_final_ts = None    # latencia del 1er final vs envío audio
        self._dg_final_started_ts = None  # inicio ventana de gracia





    # ------------------------------------------------------------------
    async def handle_twilio_websocket(self, websocket: WebSocket):
        self.websocket = websocket
        await websocket.accept()
        self._reset_all_state()
        global CURRENT_CALL_MANAGER; CURRENT_CALL_MANAGER = self
        logger.info("📞 Llamada iniciada")

        # precarga datos
        try:
            load_free_slots_to_cache(90)
            load_consultorio_data_to_cache()
        except Exception as e:
            logger.warning("⚠️ Precarga falló: %s", e, exc_info=True)

        # Deepgram
        try:
            self.stt_streamer = DeepgramSTTStreamer(self._stt_callback)
            await self.stt_streamer.start_streaming()
        except Exception as e:
            logger.error("❌ Deepgram no arrancó: %s", e, exc_info=True)
            await websocket.close(code=1011)
            return

        asyncio.create_task(self._monitor_call_timeout())

        # ciclo principal WebSocket
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







    # ------------------------------------------------------------------ helpers
    async def _send_silence_chunk(self):
        if self.stt_streamer:
            try:
                await self.stt_streamer.send_audio(b"\xff" * 320)
            except Exception as e:
                logger.debug("⚠️ No se pudo enviar silencio: %s", e)






    def _greeting(self):
        now = get_cancun_time(); h = now.hour; m = now.minute
        if 3 <= h < 12:
            return "¡Buenos días!, Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"
        if h >= 20 or h < 3 or (h == 19 and m >= 30):
            return "¡Buenas noches!, Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"
        return "¡Buenas tardes!, Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"






    # ------------------------------------------------------------------ Deepgram callback
    def _stt_callback(self, transcript: str, is_final: bool):
        if not transcript:
            return
        now = self._now()

    # Primera final de Deepgram
        if is_final and self._dg_first_final_ts is None:
            self._dg_first_final_ts = now
            logger.debug(
                "⏱️ Deepgram 1ª final tras %.0f ms",
                (now - self.stream_start_time) * 1000
            )

    # Latencia entre finals
        if is_final:
            logger.debug(
                "⏱️ Deepgram Δ final %.0f ms",
                (now - self._dg_prev_final_ts) * 1000
            )   
            self._dg_prev_final_ts = now

        # Si hay partials pendientes (ventana de gracia)
        if not is_final and self.pending_final:
            if self.final_grace_task and not self.final_grace_task.done():
                self.final_grace_task.cancel()
            new = transcript.strip()
            if new not in self.pending_final:
                self.pending_final += " " + new
                logger.debug("🟡 añadido → %s", new)
            return

        # Si es final, reinicia timer si estamos en modo teléfono
        if is_final:
            if self.accumulating_mode:
                self._cancel_accumulating_timer()
                self._start_accumulating_timer()

        # Construye o añade al pending_final
            if self.pending_final:
                new = transcript.strip()
                if new not in self.pending_final:
                    self.pending_final += " " + new
                    logger.debug("🟡 añadido → %s", new)
            else:
                self.pending_final = transcript.strip()

            # Programa el commit tras la ventana de gracia
            self._dg_final_started_ts = now
            loop = asyncio.get_event_loop()
            if self.final_grace_task and not self.final_grace_task.done():
                self.final_grace_task.cancel()
            self.final_grace_task = loop.create_task(
                self._commit_final_after_grace()
            )



    async def _commit_final_after_grace(self):
        try:
            await asyncio.sleep(self.grace_ms)
        except asyncio.CancelledError:
            return
        final_text = self.pending_final or ""
        self.pending_final = None
        logger.debug(
            "🟢 consolidado → %s  (ventana %.0f ms)",
            final_text,
            (self._now() - self._dg_final_started_ts) * 1000,
        )
        self.last_final_ts = self._now()
        if self.accumulating_mode:
            self._accumulate_transcript(final_text)
            return
        if self.current_gpt_task and not self.current_gpt_task.done():
            self.current_gpt_task.cancel()
        if self.accumulating_mode:
            asyncio.create_task(self._confirm_or_retry_phone(final_text))
        else:
            self.current_gpt_task = asyncio.create_task(self.process_gpt_response(final_text))


    # ---
    # 
    # 
    # 
    # 
    # 
    # --------------------------------------------------------------- modo teléfono
    def _activate_accumulating_mode(self):
        """Entra en modo captura-teléfono y reinicia temporizador."""
        if self.accumulating_mode:
            return
        logger.info("📞 Modo teléfono ON")
        self.accumulating_mode = True
        self.phone_attempts = 0
        self.accumulated_transcripts = []
        self._cancel_accumulating_timer()
        self._start_accumulating_timer()




    def _accumulate_transcript(self, fragment: str):
        self.accumulated_transcripts.append(fragment.strip())
        self._cancel_accumulating_timer()
        self._start_accumulating_timer()





    def _start_accumulating_timer(self):
        loop = asyncio.get_event_loop()
        self.accumulating_timer_task = loop.create_task(
            self._accumulating_timer(self.accumulating_timeout_phone)
        )

    def _cancel_accumulating_timer(self):
        if self.accumulating_timer_task and not self.accumulating_timer_task.done():
            self.accumulating_timer_task.cancel()
        self.accumulating_timer_task = None

    async def _accumulating_timer(self, timeout):
        try:
            await asyncio.sleep(timeout)
            logger.info("🟠 Timeout teléfono: flush…")
            self._flush_accumulated_transcripts()
        except asyncio.CancelledError:
            pass

    def _flush_accumulated_transcripts(self):
        if not self.accumulating_mode:
            return
        self._cancel_accumulating_timer()

        raw = " ".join(self.accumulated_transcripts).strip()
        self.accumulated_transcripts = []
        logger.info("📲 Captura teléfono (raw): %s", raw)

        # Envío al validador de 10 dígitos
        asyncio.create_task(self._confirm_or_retry_phone(raw))



    async def _confirm_or_retry_phone(self, texto_usuario: str):
        """Valida si hay 10 dígitos; si no, pide reintento."""
        digits = re.sub(r"\D", "", texto_usuario)
        if len(digits) == 10:
            fmt = ", ".join([digits[i:i+2] for i in range(0, 10, 2)])
            await self._play_audio_bytes(
                text_to_speech(f"¿Es correcto el número {fmt}?")
            )
            self.accumulating_mode = False
            return

        # Sin 10 dígitos -> reintento o fallback
        self.phone_attempts += 1
        if self.phone_attempts >= 3:
            await self._play_audio_bytes(
                text_to_speech(
                    "No logré entender el número. "
                    "¿Podría enviarlo por WhatsApp al nueve, nueve, ocho, dos, trece, setenta y cuatro, setenta y siete, por favor?"
                )
            )
            self.accumulating_mode = False
            return

        # Mensaje adaptativo según intento
        prompts = [
            "¿Podría repetir el número en dígitos corridos, por favor?",
            "Intente decirlo sin pausas o en pares, por ejemplo: noventa y nueve, ochenta y dos..."
        ]
        await self._play_audio_bytes(text_to_speech(prompts[self.phone_attempts-1]))
        await asyncio.sleep(0.5)
        self._start_accumulating_timer()


    # ------------------------------------------------------------------ bucle detector (opcional)
    def _detectar_bucle(self, gpt_response: str) -> bool:
        """
        Revisa si la IA está repitiendo demasiado seguido una frase o patrón.
        Por ahora se usa una comparación simple de las últimas 3 respuestas.
        """
        if len(self.conversation_history) < 6:
            return False  # Aún no hay suficiente historial

        ultimas_ia = [
            m["content"].strip().lower()
            for m in self.conversation_history
            if m["role"] == "assistant"
        ][-3:]  # últimas 3 respuestas de IA

        # si las 3 últimas son iguales (o muy parecidas), hay bucle
        if len(set(ultimas_ia)) <= 1:
            logger.warning("🔁 Posible bucle detectado: misma respuesta repetida")
            return True

        return False








    # ------------------------------------------------------------------ GPT round‑trip
    async def process_gpt_response(self, user_text: str):
        if self.call_ended or not self.websocket or self.websocket.client_state != WebSocketState.CONNECTED:
            return

        # 1. Añadimos el mensaje del usuario al historial
        user_input = {"role": "user", "content": f"[ES] {user_text}"}
        if not self.conversation_history or self.conversation_history[-1]["content"] != user_input["content"]:
            self.conversation_history.append(user_input)

        # 2. Construimos el prompt y llamamos a GPT
        messages_for_gpt = generate_openai_prompt(self.conversation_history)
        t0 = self._now()
        gpt_response = await generate_openai_response_main(messages_for_gpt, model="gpt-4.1-mini")

        # 3. Si la IA ya pregunta por el motivo, salimos de modo teléfono
        if "motivo" in gpt_response.lower():
            self.accumulating_mode = False

        logger.debug("⏱️ GPT %.0f ms", (self._now() - t0) * 1000)

        # 4. Si es llamada a colgar, cerramos todo
        if gpt_response == "__END_CALL__":
            await self._shutdown()
            return

        # 5. Almacenamos la respuesta de la IA en el historial
        self.conversation_history.append({"role": "assistant", "content": gpt_response})
        logger.info("🤖 IA: %s", gpt_response)

    # 6. Prevención de bucles
        if self._detectar_bucle(gpt_response):
            await self._shutdown()
            return

    # 7. Generamos audio con ElevenLabs
        t1 = self._now()
        audio = text_to_speech(gpt_response)
        logger.debug("⏱️ ElevenLabs %.0f ms", (self._now() - t1) * 1000)

    # 8. Detectar frase que activa modo teléfono
        phone_trigger_phrases = [
            "¿me puede compartir el número de whatsapp para enviarle la confirmación, por favor?",
            "me podría repetir el número de teléfono por favor?",
            "me podría compartir el número de teléfono con el que se hizo la cita originalmente por favor, de esta manera puedo localizar la cita en el calendario?"
        ]
        for phrase in phone_trigger_phrases:
            if phrase in gpt_response.lower():
                logger.info(f"📞 Activación modo teléfono: frase detectada → “{phrase}”")
                asyncio.create_task(self._activate_accumulating_mode_after_audio())
                break

    # 9. Enviamos el audio a Twilio
        self.is_speaking = True
        await self._play_audio_bytes(audio)
        self.is_speaking = False

    # 10. Si la IA se despide, colgamos
        if GOODBYE_PHRASE.lower() in gpt_response.lower():
            await self._shutdown()
            return

    # 11. Un pequeño colchón de silencio para el STT
        await asyncio.sleep(0.2)
        await self._send_silence_chunk()


    # ------------------------------------------------------------------ audio → Twilio
    async def _play_audio_bytes(self, audio_data: bytes):
        if not audio_data or not self.websocket or self.websocket.client_state != WebSocketState.CONNECTED:
            return
        total_len = len(audio_data)
        logger.debug("\U0001f4e4 Audio %d\u00a0B", total_len)

        chunk_size = 512
        fast = total_len <= 24000
        per_chunk_delay = 0.03 if fast else chunk_size / 8000.0

        t_send = self._now()
        offset = 0
        while offset < total_len and not self.call_ended:
            chunk = audio_data[offset: offset + chunk_size]
            offset += chunk_size
            await self.websocket.send_text(
                json.dumps({
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {"payload": base64.b64encode(chunk).decode()},
                })
            )
            await asyncio.sleep(per_chunk_delay)
        logger.debug("⏱️ Audio enviado %.0f ms", (self._now() - t_send) * 1000)


        # ⚡ Esperar un poquito menos de lo normal
        await asyncio.sleep(max(0, 1.0 - per_chunk_delay))  # <= Liberamos 1 seg antes de terminar "completamente"


    # ------------------------------------------------------------------ shutdown & watchdog
    async def _shutdown(self):
        if self.call_ended:
            return
        self.call_ended = True
        logger.info("🔻 Cuelga llamada")

        if not any(GOODBYE_PHRASE.lower() in m["content"].lower() for m in self.conversation_history if m["role"] == "assistant"):
            logger.info("📢 Despedida no detectada, enviando mensaje personalizado antes de colgar.")
            despedida_final = "Gracias por comunicarte al consultorio del doctor Wilfrido Alarcón. ¡Hasta pronto!"
            await self._play_audio_bytes(text_to_speech(despedida_final))


        if self.stt_streamer:
            await self.stt_streamer.close()
        if self.websocket and self.websocket.client_state == WebSocketState.CONNECTED:
            await self.websocket.close()
        self._reset_all_state()

    async def _monitor_call_timeout(self):
        while not self.call_ended:
            await asyncio.sleep(2)
            if self._now() - self.stream_start_time >= CALL_MAX_DURATION:
                logger.info("⏰ Duración máxima excedida")
                await self._shutdown(); return
            if self._now() - self.last_final_ts >= CALL_SILENCE_TIMEOUT:
                logger.info("🛑 Silencio prolongado")
                await self._shutdown(); return
            await self._send_silence_chunk()
