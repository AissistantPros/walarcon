# tw_utils.py

"""
Este archivo gestiona toda la lógica de llamada con Twilio WebSocket.
Ajustes 25abr2025
────────────────────────────
• Cambio a `update_options` (SDK Deepgram v1) — antes usábamos `.configure()`
• Deduplicación de texto en `_stt_callback` para evitar repeticiones.
• Se comenta el log del modelo GPT para menos ruido.
• Se evita apilar dos veces el mismo mensaje en `conversation_history`.
"""

import json, base64, time, asyncio, logging
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
CALL_MAX_DURATION   = 600
CALL_SILENCE_TIMEOUT= 30

GOODBYE_PHRASE = "Fue un placer atenderle. Que tenga un excelente día. ¡Hasta luego!"

class TwilioWebSocketManager:
    def __init__(self):
        self.accumulating_timeout_general = 1.0
        self.accumulating_timeout_phone   = 3.5
        self.grace_ms = 0.6
        self.accumulating_timer_task = None
        self.final_grace_task        = None
        self._reset_all_state()

    # ──────────────────────────────────────────────────────────
    def _reset_all_state(self):
        logger.info("🧼 Reiniciando TODAS las variables internas del sistema.")
        self.call_ended          = False
        self.conversation_history= []
        self.current_language    = "es"
        self.expecting_number    = False
        self.expecting_name      = False
        # Anti‑cortes Deepgram
        if self.final_grace_task and not self.final_grace_task.done():
            self.final_grace_task.cancel()
        self.final_grace_task = None
        self.pending_final    = None
        # Acumulación teléfono
        self.accumulating_mode       = False
        self.accumulated_transcripts = []
        self._cancel_accumulating_timer()
        # Referencias runtime
        self.current_gpt_task = None
        self.stt_streamer     = None
        self.is_speaking      = False
        self.stream_sid       = None
        self.websocket        = getattr(self, "websocket", None)
        now = time.time()
        self.stream_start_time= now
        self.last_partial_time= now
        self.last_final_time  = now

    # ──────────────────────────────────────────────────────────
    async def handle_twilio_websocket(self, websocket: WebSocket):
        self.websocket = websocket
        await websocket.accept()
        self._reset_all_state()
        global CURRENT_CALL_MANAGER; CURRENT_CALL_MANAGER = self
        logger.info("📞 Llamada iniciada.")
        # Precarga cachés
        try:
            load_free_slots_to_cache(90)
            load_consultorio_data_to_cache()
        except Exception as e:
            logger.error("❌ Error precargando datos: %s", e, exc_info=True)
        # Deepgram
        try:
            self.stt_streamer = DeepgramSTTStreamer(self._stt_callback)
            await self.stt_streamer.start_streaming()
        except Exception as e:
            logger.error("❌ Error iniciando STT: %s", e, exc_info=True)
            await websocket.close(code=1011)
            return
        asyncio.create_task(self._monitor_call_timeout())
        try:
            while True:
                raw = await websocket.receive_text()
                data = json.loads(raw)
                evt  = data.get("event")
                if evt == "start":
                    self.stream_sid = data.get("streamSid", "")
                    saludo_audio = text_to_speech(self._get_greeting_by_time())
                    await self._play_audio_bytes(saludo_audio)
                elif evt == "media":
                    if self.is_speaking: continue
                    payload = data["media"].get("payload")
                    if payload:
                        await self.stt_streamer.send_audio(base64.b64decode(payload))
                elif evt == "stop":
                    logger.info("🛑 Evento 'stop' recibido desde Twilio.")
                    break
        except Exception as e:
            logger.error("❌ WebSocket error: %s", e, exc_info=True)
        finally:
            await self._shutdown()

    # ────────────────────────────────────────────────────────── helpers
    async def _send_silence_chunk(self):
        if self.stt_streamer:
            try:
                await self.stt_streamer.send_audio(b"\xff"*320)
            except Exception as e:
                logger.warning("⚠️ Error al enviar silencio: %s", e)

    def _farewell_already_sent(self):
        for msg in reversed(self.conversation_history[-6:]):
            if msg["role"]=="assistant" and GOODBYE_PHRASE.lower() in msg["content"].lower():
                return True
        return False

    def _get_greeting_by_time(self):
        now=get_cancun_time(); h=now.hour; m=now.minute
        if 3<=h<12:  return "¡Buenos días!, Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"
        if h>=20 or h<3 or (h==19 and m>=30):
            return "¡Buenas noches!, Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"
        return "¡Buenas tardes!, Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"

    # ────────────────────────────────────────────────────────── Deepgram callback
    def _stt_callback(self, transcript:str, is_final:bool):
        if not transcript: return
        self.last_partial_time = time.time()
        # Partial mientras hay final en gracia
        if not is_final and self.pending_final:
            if self.final_grace_task and not self.final_grace_task.done():
                self.final_grace_task.cancel()
            new = transcript.strip()
            if new not in self.pending_final:  # dedupe simple
                self.pending_final += " " + new
            return
        # Final
        if is_final:
            if self.pending_final:
                new = transcript.strip()
                if new not in self.pending_final:
                    self.pending_final += " " + new
                if self.final_grace_task and not self.final_grace_task.done():
                    self.final_grace_task.cancel()
            else:
                self.pending_final = transcript.strip()
            loop = asyncio.get_event_loop()
            self.final_grace_task = loop.create_task(self._commit_final_after_grace())

    async def _commit_final_after_grace(self):
        try:
            await asyncio.sleep(self.grace_ms)
        except asyncio.CancelledError:
            return
        final_text = self.pending_final; self.pending_final = None
        self.last_final_time = time.time()
        logger.info("🎙️ USUARIO (final + grace): %s", final_text)
        if self.accumulating_mode:
            self._accumulate_transcript(final_text); return
        if self.current_gpt_task and not self.current_gpt_task.done():
            self.current_gpt_task.cancel()
        self.current_gpt_task = asyncio.create_task(self.process_gpt_response(final_text))

    # ────────────────────────────────────────────────────────── acumulación teléfono
    def _activate_accumulating_mode(self):
        if self.accumulating_mode: return
        logger.info("📞 Activando modo acumulación (teléfono).")
        self.accumulating_mode=True; self.accumulated_transcripts=[]
        asyncio.create_task(
            self.stt_streamer.dg_connection.update_options({
                "endpointing": False,
                "utterance_end_ms": "6000",
            })
        )
        self._start_accumulating_timer(True)

    def _accumulate_transcript(self, fragment:str):
        self.accumulated_transcripts.append(fragment.strip())
        self._cancel_accumulating_timer(); self._start_accumulating_timer(True)

    def _start_accumulating_timer(self, phone_mode):
        loop=asyncio.get_event_loop()
        timeout = self.accumulating_timeout_phone if phone_mode else self.accumulating_timeout_general
        self.accumulating_timer_task = loop.create_task(self._accumulating_timer(timeout))
        #logger.info("⏳ Temporizador acumulación iniciado (%.1fs).", timeout)

    def _cancel_accumulating_timer(self):
        if self.accumulating_timer_task and not self.accumulating_timer_task.done():
            self.accumulating_timer_task.cancel()
        self.accumulating_timer_task=None

    async def _accumulating_timer(self, timeout):
        try:
            await asyncio.sleep(timeout); logger.info("🟠 Timeout acumulación: flusheando…")
            self._flush_accumulated_transcripts()
        except asyncio.CancelledError:
            pass

    def _flush_accumulated_transcripts(self):
        if not self.accumulating_mode: return
        self._cancel_accumulating_timer()
        raw = " ".join(self.accumulated_transcripts).strip()
        digits="".join(ch for ch in raw if ch.isdigit())
        non   = "".join(ch for ch in raw if not ch.isdigit()).strip()
        if "?" in non or (non and len(digits)<4):
            logger.info("❓ Comentario/pregunta detectado → salgo de modo teléfono.")
            self.accumulating_mode=False; self.accumulated_transcripts=[]
            asyncio.create_task(self.stt_streamer.dg_connection.update_options({"endpointing":False,"utterance_end_ms":"4000"}))
            if self.current_gpt_task and not self.current_gpt_task.done():
                self.current_gpt_task.cancel()
            self.current_gpt_task = asyncio.create_task(self.process_gpt_response(raw))
            return
        if len(digits)<10:
            logger.info("🔄 Aún <10 dígitos; sigo esperando…")
            self._start_accumulating_timer(True); return
        self.accumulating_mode=False
        numero=" ".join(digits); logger.info("📞 Número capturado: %s", numero)
        if self.current_gpt_task and not self.current_gpt_task.done():
            self.current_gpt_task.cancel()
        self.current_gpt_task = asyncio.create_task(self.process_gpt_response(numero))
        asyncio.create_task(self.stt_streamer.dg_connection.update_options({"endpointing":False,"utterance_end_ms":"4000"}))

    # ────────────────────────────────────────────────────────── GPT round‑trip
    async def process_gpt_response(self, user_text:str):
        if self.call_ended or not self.websocket or self.websocket.client_state!=WebSocketState.CONNECTED:
            return
        user_input = {"role":"user","content":f"[ES] {user_text}"}
        # Evita duplicar
        if not (self.conversation_history and self.conversation_history[-1]["content"]==user_input["content"]):
            self.conversation_history.append(user_input)
        messages_for_gpt = generate_openai_prompt(self.conversation_history)
        model="gpt-4.1-mini"
        # logger.info(f"⌛ Se utilizará el modelo: {model} para el texto: {user_text}")  # desactivado
        gpt_response = await generate_openai_response_main(messages_for_gpt, model=model)
        if gpt_response=="__END_CALL__": await self._shutdown(); return
        self.conversation_history.append({"role":"assistant","content":gpt_response})
        logger.info("🤖 IA (texto completo): %s", gpt_response)
        resp_lower=gpt_response.lower()
        if "número de whatsapp" in resp_lower: self._activate_accumulating_mode()
        if GOODBYE_PHRASE.lower() in resp_lower:
            self.is_speaking=True; await self._play_audio_bytes(text_to_speech(gpt_response))
            await asyncio.sleep(0.2); self.is_speaking=False; await self._shutdown(); return
        self.is_speaking=True; await self._play_audio_bytes(text_to_speech(gpt_response))
        await asyncio.sleep(0.2); self.is_speaking=False; await self._send_silence_chunk()









    async def _play_audio_bytes(self, audio_data: bytes):
        """
        Envía audio TTS a Twilio en chunks de 1024 B.

        ▸ Si el audio completo es ≤ 24 000 B (≈ 3 s a 8 kHz mu-law),
          lo subimos 4× más rápido (delay = 0.03 s) para minimizar
          la latencia de respuesta percibida.

        ▸ Para audios más largos usamos tiempo-real
          (delay = chunk_size / 8000 ≈ 0.128 s) para no saturar
          ancho de banda ni memoria en Twilio.
        """
        if not audio_data:
            logger.warning("❌ No hay audio para reproducir.")
            return
        if not self.websocket or self.websocket.client_state != WebSocketState.CONNECTED:
            logger.warning("❌ WebSocket no conectado. No se puede enviar audio.")
            return

        total_len = len(audio_data)
        logger.info(f"📤 Enviando audio a Twilio ({total_len} bytes)...")

        chunk_size = 1024
        # ── Delay adaptativo ───────────────────────────────────────────────
        if total_len <= 24000:                 # ≈ 3 s de audio
            per_chunk_delay = 0.03             # 4 × más rápido
        else:
            per_chunk_delay = chunk_size / 8000.0   # tiempo-real ≈ 0.128 s

        # ── Envío de chunks ───────────────────────────────────────────────
        offset = 0
        while offset < total_len and not self.call_ended:
            chunk = audio_data[offset:offset + chunk_size]
            offset += chunk_size
            base64_chunk = base64.b64encode(chunk).decode("utf-8")
            message = {
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": base64_chunk},
            }
            try:
                await self.websocket.send_text(json.dumps(message))
                await asyncio.sleep(per_chunk_delay)
            except Exception as e:
                logger.error(f"⚠️ Error enviando audio: {e}")
                break










    async def _shutdown(self):
        """
        Cierra de forma ordenada la llamada:
        ▸ Detiene el streamer de Deepgram.
        ▸ Cierra el WebSocket de Twilio (si sigue abierto).
        ▸ Limpia todas las variables internas.
        """
        # Evita doble ejecución
        if self.call_ended:
            return

        self.call_ended = True
        self.accumulating_mode = False   # ← garantizamos que el modo teléfono quede inactivo

        # ── Reproducir despedida si aún no se dijo ─────────────
        if not self._farewell_already_sent():
            logger.info("🔊 Despedida no encontrada; reproduciendo antes de colgar.")
            self.is_speaking = True
            bye_audio = text_to_speech(GOODBYE_PHRASE)
            await self._play_audio_bytes(bye_audio)
            await asyncio.sleep(0.2)   # colchón corto
            self.is_speaking = False

        logger.info("🔻 Terminando la llamada...")

        # 1. Cerrar Deepgram
        if self.stt_streamer:
            try:
                await self.stt_streamer.close()
            except Exception as e:
                logger.warning(f"⚠️ Error cerrando Deepgram: {e}")

        # 2. Cerrar WebSocket con Twilio
        if self.websocket and self.websocket.client_state == WebSocketState.CONNECTED:
            try:
                await self.websocket.close()
            except Exception as e:
                logger.warning(f"⚠️ Error cerrando WebSocket: {e}")

        # 3. Limpiar variables
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

