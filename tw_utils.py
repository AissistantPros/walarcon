# tw_utils.py

"""
Este archivo gestiona toda la lógica de llamada con Twilio WebSocket.

- Cada vez que el usuario habla, se envía:
  - La hora actual en Cancún como system_message temporal.
  - El mensaje del usuario.
  - El historial anterior sin system_messages.

- La IA puede consultar también la hora con la tool get_cancun_time si lo necesita.

- El historial real mantiene solo las intervenciones de usuario e IA.

- La lógica de acumulación permite recoger números largos como teléfonos sin cortes.

Autor: Esteban Reyna / Aissistants Pro
"""


import json
import base64
import time
import asyncio
import logging
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

CALL_MAX_DURATION = 600
CALL_SILENCE_TIMEOUT = 30

# ── Despedida obligatoria ───────────────────────────────────────────
GOODBYE_PHRASE = (
    "Fue un placer atenderle. Que tenga un excelente día. ¡Hasta luego!"
)



class TwilioWebSocketManager:
    def __init__(self):
        # ­── timeouts configurables
        self.accumulating_timeout_general = 1.0   # conversación normal
        self.accumulating_timeout_phone   = 3.5   # modo teléfono
        self.grace_ms = 0.6                      # margen anticípate-cortes Deepgram

        # temporizadores/tareas
        self.accumulating_timer_task = None
        self.final_grace_task        = None

        # inicializa todo lo demás
        self._reset_all_state()

    # ---------------------------------------------------------
    # Re-inicializa **todas** las variables internas
    # ---------------------------------------------------------
    def _reset_all_state(self):
        logger.info("🧼 Reiniciando TODAS las variables internas del sistema.")

        # ­── estado global de la llamada
        self.call_ended        = False
        self.conversation_history = []
        self.current_language  = "es"

        # ­── flags de la lógica de pasos
        self.expecting_number  = False
        self.expecting_name    = False

        # ­── anticípate-cortes (Deepgram)
        if self.final_grace_task and not self.final_grace_task.done():
            self.final_grace_task.cancel()
        self.final_grace_task = None
        self.pending_final    = None   # texto final que está “a prueba”

        # ­── modo acumulación teléfono
        self.accumulating_mode      = False
        self.accumulated_transcripts = []
        self._cancel_accumulating_timer()  # cancela si existía

        # ­── referencias a tareas/objetos activos
        self.current_gpt_task = None
        self.stt_streamer     = None
        self.is_speaking      = False
        self.stream_sid       = None
        self.websocket        = getattr(self, "websocket", None)  # puede no existir

        # ­── control de tiempos
        now = time.time()
        self.stream_start_time = now
        self.last_partial_time = now
        self.last_final_time   = now


    async def handle_twilio_websocket(self, websocket: WebSocket):
        self.websocket = websocket
        await websocket.accept()
        self._reset_all_state()

        global CURRENT_CALL_MANAGER
        CURRENT_CALL_MANAGER = self

        logger.info("📞 Llamada iniciada.")
        

        try:
            load_free_slots_to_cache(90)
            load_consultorio_data_to_cache()
        except Exception as e:
            logger.error(f"❌ Error precargando datos: {e}", exc_info=True)

        try:
            self.stt_streamer = DeepgramSTTStreamer(self._stt_callback)
            await self.stt_streamer.start_streaming()
        except Exception as e:
            logger.error(f"❌ Error iniciando STT: {e}", exc_info=True)
            await websocket.close(code=1011)
            return

        asyncio.create_task(self._monitor_call_timeout())

        try:
            while True:
                raw_msg = await websocket.receive_text()
                data = json.loads(raw_msg)
                event_type = data.get("event")

                if event_type == "start":
                    self.stream_sid = data.get("streamSid", "")
                    saludo = self._get_greeting_by_time()
                    saludo_audio = text_to_speech(saludo)
                    await self._play_audio_bytes(saludo_audio)

                elif event_type == "media":
                    if self.is_speaking:
                        continue
                    payload = data["media"].get("payload")
                    if payload:
                        audio_chunk = base64.b64decode(payload)
                        await self.stt_streamer.send_audio(audio_chunk)

                elif event_type == "stop":
                    logger.info("🛑 Evento 'stop' recibido desde Twilio.")
                    break

        except Exception as e:
            logger.error(f"❌ WebSocket error: {e}", exc_info=True)
        finally:
            await self._shutdown()









    async def _send_silence_chunk(self):
        if self.stt_streamer:
            try:
                silence = b'\xff' * 320
                await self.stt_streamer.send_audio(silence)
            except Exception as e:
                logger.warning(f"⚠️ Error al enviar silencio: {e}")


    # ──────────────────────────────────────────────────────────
    #  Helper: ¿ya se dijo la despedida?
    # ──────────────────────────────────────────────────────────
    def _farewell_already_sent(self) -> bool:
        """
        Revisa las últimas intervenciones de la IA para saber
        si ya pronunció la frase de despedida obligatoria.
        """
        for msg in reversed(self.conversation_history[-6:]):  # mira los últimos 6 turnos
            if (
                msg["role"] == "assistant"
                and GOODBYE_PHRASE.lower() in msg["content"].lower()
            ):
                return True
        return False











    def _get_greeting_by_time(self):
        now = get_cancun_time()
        hour, minute = now.hour, now.minute
        if 3 <= hour < 12:
            return "¡Buenos días!, Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"
        elif hour >= 20 or hour < 3 or (hour == 19 and minute >= 30):
            return "¡Buenas noches!, Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"
        else:
            return "¡Buenas tardes!, Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"












       # ──────────────────────────────────────────────────────────
    #  CALLBACK DE DEEPGRAM  (partial / final)
    # ──────────────────────────────────────────────────────────
    def _stt_callback(self, transcript: str, is_final: bool) -> None:
        """
        Recibe eventos de Deepgram:
          • partial  → actualiza last_partial_time y, si hay un final “en-gracia”,
                        lo cancela y concatena el nuevo texto.
          • final    → inicia (o reinicia) temporizador de gracia; si llega otro 
                        fragmento dentro del margen, se juntan antes de procesar.
        """
        if not transcript:
            return

        # Timestamp del último audio recibido
        self.last_partial_time = time.time()

        # ── Caso: llega un partial mientras hay final pendiente ────────────
        if not is_final and self.pending_final:
            # cancela el temporizador de gracia
            if self.final_grace_task and not self.final_grace_task.done():
                self.final_grace_task.cancel()

            # concatena el texto nuevo
            self.pending_final += " " + transcript.strip()
            return  # no hacemos nada más hasta que vuelva un final

        # ── Caso: llega un final ───────────────────────────────────────────
        if is_final:
            if self.pending_final:
                # ya había un final en espera → lo extendemos
                self.pending_final += " " + transcript.strip()
                if self.final_grace_task and not self.final_grace_task.done():
                    self.final_grace_task.cancel()
            else:
                # primer final recibido
                self.pending_final = transcript.strip()

            # inicia / reinicia el temporizador de gracia
            loop = asyncio.get_event_loop()
            self.final_grace_task = loop.create_task(
                self._commit_final_after_grace()
            )

    async def _commit_final_after_grace(self) -> None:
        """
        Se ejecuta si pasa self.grace_ms sin que llegue otro fragmento.
        Considera la frase como final definitiva y la envía a GPT
        (o al acumulador de teléfono, según corresponda).
        """
        try:
            await asyncio.sleep(self.grace_ms)
        except asyncio.CancelledError:
            return  # interrumpido porque llegó texto adicional

        # — final consolidado —
        final_text = self.pending_final
        self.pending_final = None
        self.last_final_time = time.time()

        logger.info(f"🎙️ USUARIO (final + grace): {final_text}")

        # Si estamos recogiendo número de teléfono, acumular
        if self.accumulating_mode:
            self._accumulate_transcript(final_text)
            return

        # Cancela cualquier request GPT en curso
        if self.current_gpt_task and not self.current_gpt_task.done():
            self.current_gpt_task.cancel()

        # Lanza nueva petición GPT
        self.current_gpt_task = asyncio.create_task(
            self.process_gpt_response(final_text)
        )






    # ──────────────────────────────────────────────────────────
    #  MODO ACUMULACIÓN PARA NÚMEROS DE TELÉFONO
    # ──────────────────────────────────────────────────────────
    def _activate_accumulating_mode(self) -> None:
        """
        Activa un modo temporal para capturar números con pausas.
        • Deshabilita endpointing por silencio.
        • Alarga utterance_end_ms a 6 s.
        • Inicia el temporizador de 3.5 s (self.accumulating_timeout_phone).
        """
        if self.accumulating_mode:
            return  # ya estaba activo

        logger.info("📞 Activando modo acumulación (teléfono).")
        self.accumulating_mode = True
        self.accumulated_transcripts = []

        # Ajustar configuración de Deepgram “en caliente”
        asyncio.create_task(
            self.stt_streamer.dg_connection.configure(
                endpointing=False,          # no cortes por VAD-silencio
                utterance_end_ms="6000"     # 6 s sin tokens = final
            )
        )

        # Primer temporizador
        self._start_accumulating_timer(phone_mode=True)

    # ----------------------------------------------------------------------
    def _accumulate_transcript(self, fragment: str) -> None:
        """
        Guarda fragmentos finales y reinicia el temporizador cada vez.
        """
        self.accumulated_transcripts.append(fragment.strip())
        logger.debug(f"➕ Fragmento acumulado: {fragment.strip()}")

        # Reinicia temporizador
        self._cancel_accumulating_timer()
        self._start_accumulating_timer(phone_mode=True)

    # ----------------------------------------------------------------------
    def _start_accumulating_timer(self, phone_mode: bool) -> None:
        loop = asyncio.get_event_loop()
        timeout = (
            self.accumulating_timeout_phone
            if phone_mode
            else self.accumulating_timeout_general
        )
        self.accumulating_timer_task = loop.create_task(
            self._accumulating_timer(timeout)
        )
        logger.info(f"⏳ Temporizador acumulación iniciado ({timeout}s).")

    def _cancel_accumulating_timer(self) -> None:
        if self.accumulating_timer_task and not self.accumulating_timer_task.done():
            self.accumulating_timer_task.cancel()
        self.accumulating_timer_task = None

    # ----------------------------------------------------------------------
    async def _accumulating_timer(self, timeout: float) -> None:
        try:
            await asyncio.sleep(timeout)
            logger.info("🟠 Timeout acumulación: flusheando…")
            self._flush_accumulated_transcripts()
        except asyncio.CancelledError:
            logger.debug("🔁 Temporizador acumulación cancelado/reiniciado.")

    # ----------------------------------------------------------------------
    def _flush_accumulated_transcripts(self) -> None:
        """
        Lógica de salida del modo teléfono.

        • Si detecta signos de pregunta o texto no-numérico predominante,
          sale del modo teléfono y re-envía la frase a GPT como conversación.
        • Si tiene <10 dígitos → sigue esperando (reinicia temporizador).
        • Si ≥10 dígitos → envía el número limpio a GPT y vuelve al modo normal.
        """
        if not self.accumulating_mode:
            return

        # Detiene temporizador
        self._cancel_accumulating_timer()

        raw_text = " ".join(self.accumulated_transcripts).strip()
        digits_only = "".join(ch for ch in raw_text if ch.isdigit())
        non_digits = "".join(ch for ch in raw_text if not ch.isdigit()).strip()

        # 1) Pregunta / comentario
        if "?" in non_digits or (non_digits and len(digits_only) < 4):
            logger.info("❓ Comentario/pregunta detectado → salgo de modo teléfono.")
            self.accumulating_mode = False
            self.accumulated_transcripts = []

            # Restaurar configuración estándar de Deepgram
            asyncio.create_task(
                self.stt_streamer.dg_connection.configure(
                    endpointing=False,
                    utterance_end_ms="4000",
                )
            )

            # Reenviar al flujo normal de GPT
            if self.current_gpt_task and not self.current_gpt_task.done():
                self.current_gpt_task.cancel()
            self.current_gpt_task = asyncio.create_task(
                self.process_gpt_response(raw_text)
            )
            return

        # 2) Aún no hay número completo
        if len(digits_only) < 10:
            logger.info("🔄 Aún <10 dígitos; sigo esperando…")
            self._start_accumulating_timer(phone_mode=True)
            return

        # 3) Número completo
        self.accumulating_mode = False
        numero_formateado = " ".join(digits_only)
        logger.info(f"📞 Número capturado: {numero_formateado}")

        # Cancelar GPT previo si existía
        if self.current_gpt_task and not self.current_gpt_task.done():
            self.current_gpt_task.cancel()

        # Enviar número a GPT
        self.current_gpt_task = asyncio.create_task(
            self.process_gpt_response(numero_formateado)
        )

        # Restaurar Deepgram a configuración estándar
        asyncio.create_task(
            self.stt_streamer.dg_connection.configure(
                endpointing=False,
                utterance_end_ms="4000",
            )
        )



    async def process_gpt_response(self, user_text: str):
        """
        Envía el texto del usuario al modelo, reproduce la respuesta con TTS
        y reactiva la escucha inmediatamente después de terminar de enviar el
        audio a Twilio (se elimina la espera proporcional al tamaño del audio).

        Cambios clave:
        ▸ Se sustituye el sleep basado en len(audio)/6400 por un colchón fijo de 200 ms.
        ▸ El divisor erróneo 6400 se descarta: el bucle _play_audio_bytes ya
          envía en tiempo real (1024 B → 128 ms).  
        ▸ Después del colchón se baja is_speaking y se envía un frame de silencio
          para que Deepgram abra un nuevo endpoint sin latencia.
        """
        if self.call_ended or not self.websocket or self.websocket.client_state != WebSocketState.CONNECTED:
            return

        user_lang = "ES"
        self.current_language = user_lang

        user_input = {"role": "user", "content": f"[{user_lang}] {user_text}"}
        messages_for_gpt = generate_openai_prompt(self.conversation_history + [user_input])

        model = "gpt-4.1-mini"
        logger.info(f"⌛ Se utilizará el modelo: {model} para el texto: {user_text}")

        gpt_response = await generate_openai_response_main(messages_for_gpt, model=model)

        if gpt_response == "__END_CALL__":
            await self._shutdown()
            return

        # ── Guardar en historial ───────────────────────────────────────────────
        self.conversation_history.append(user_input)
        self.conversation_history.append({"role": "assistant", "content": gpt_response})

        logger.info(f"🤖 IA (texto completo): {gpt_response}")
        resp_lower = gpt_response.lower()

        # ── Flags para modo teléfono (número) ──────────────────────────────────
        if "número de whatsapp" in resp_lower:
            self._activate_accumulating_mode()
            self.expecting_number = True
            self.expecting_name = False
        elif any(kw in resp_lower for kw in ["¿es correcto", "¿cuál es el motivo", "¿confirmamos"]):
            self.expecting_number = False
            self.expecting_name = False

        # ── Despedida final ────────────────────────────────────────────────────
        if "fue un placer atenderle. que tenga un excelente día. ¡hasta luego!" in resp_lower:
            logger.info("🧼 Frase de cierre detectada. Reproduciendo y terminando llamada.")
            self.is_speaking = True
            tts_audio = text_to_speech(gpt_response)
            await self._play_audio_bytes(tts_audio)
            await asyncio.sleep(0.2)            # pequeño colchón
            self.is_speaking = False
            await self._shutdown()
            return

        # ── Reproducir respuesta normal ───────────────────────────────────────
        self.is_speaking = True
        tts_audio = text_to_speech(gpt_response)
        await self._play_audio_bytes(tts_audio)

        # colchón corto para no cortar la última sílaba
        await asyncio.sleep(0.2)

        # volver a escuchar
        self.is_speaking = False

        # frame de silencio → Deepgram detecta fin de locución y reabre endpoint
        await self._send_silence_chunk()










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

