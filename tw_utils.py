# tw_utils.py

import json
import base64
import time
import asyncio
import logging
from fastapi import WebSocket
from starlette.websockets import WebSocketState
from langdetect import detect
from consultarinfo import load_consultorio_data_to_cache
from deepgram_stt_streamer import DeepgramSTTStreamer
from aiagent import generate_openai_response
from tts_utils import text_to_speech
from utils import get_cancun_time
from buscarslot import load_free_slots_to_cache, free_slots_cache, last_cache_update

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

CURRENT_CALL_MANAGER = None

# Ajusta según tu preferencia
CALL_MAX_DURATION = 600     # 10 min
CALL_SILENCE_TIMEOUT = 30   # 30 segundos de silencio total

class TwilioWebSocketManager:
    def __init__(self):
        self.call_ended = False
        self.stream_sid = None
        self.stt_streamer = None

        # Control de tiempo
        self.stream_start_time = time.time()
        self.last_partial_time = time.time()  # actualiza cada vez que recibimos algo
        self.last_final_time = time.time()    # actualiza cuando algo se marca final

        self.websocket = None
        self.is_speaking = False
        self.conversation_history = []
        self.current_gpt_task = None

        # Banderas clásicas
        self.expecting_number = False
        self.expecting_name = False

        self.current_language = "es"  # Inicialmente en español


        # ─────────────────────────────────────────────────────────
        # NUEVAS VARIABLES PARA “MODO ACUMULACIÓN” DE TRANSCRIPCIONES
        # ─────────────────────────────────────────────────────────
        self.accumulating_mode = False           # True cuando queremos juntar transcripciones
        self.accumulated_transcripts = []        # Lista de strings con las partes finales
        self.accumulating_timer_task = None      # Tarea asyncio que “espera 4s” para procesar
        self.accumulating_timeout_seconds = 4.0  # Ajusta a gusto

    def _detect_language(self, text: str) -> str:
        """
        Detecta el idioma del texto usando langdetect.
        Retorna 'en' si es inglés, de lo contrario 'es'.
        """
        try:
            # Podemos agregar lógica adicional si queremos usar marcadores específicos
            return detect(text)
        except Exception:
            return "es"




    async def handle_twilio_websocket(self, websocket: WebSocket):
        """
        Punto de entrada para manejar el WebSocket enviado por Twilio <Stream>.
        """
        self.websocket = websocket
        await websocket.accept()

        global CURRENT_CALL_MANAGER
        CURRENT_CALL_MANAGER = self

        logger.info("📞 Llamada iniciada.")

        # Precargar datos
        try:
            load_free_slots_to_cache(90)
            load_consultorio_data_to_cache()
        except Exception as e:
            logger.error(f"❌ Error precargando datos: %s", e, exc_info=True)

        # Iniciar STT
        try:
            self.stt_streamer = DeepgramSTTStreamer(self._stt_callback)
            await self.stt_streamer.start_streaming()
        except Exception as e:
            logger.error(f"❌ Error iniciando STT: %s", e, exc_info=True)
            await websocket.close(code=1011)
            return

        # Crear tarea que chequea silencio total o tiempo máximo
        asyncio.create_task(self._monitor_call_timeout())

        # Recibir datos WebSocket (audio en tiempo real de Twilio)
        try:
            while True:
                raw_msg = await websocket.receive_text()
                data = json.loads(raw_msg)
                event_type = data.get("event")

                if event_type == "start":
                    self.stream_sid = data.get("streamSid", "")
                    # Saludo inicial
                    saludo = self._get_greeting_by_time()
                    saludo_audio = text_to_speech(saludo)
                    await self._play_audio_bytes(saludo_audio)

                elif event_type == "media":

                    # NUEVO: Si el sistema está hablando (modo mute), ignorar el audio entrante
                    if self.is_speaking:
                        logger.info("🔇 Modo mute activo: se ignora audio entrante.")
                        continue

                    payload = data["media"].get("payload")
                    if payload:
                        audio_chunk = base64.b64decode(payload)
                        await self.stt_streamer.send_audio(audio_chunk)

                elif event_type == "stop":
                    logger.info("🛑 Evento 'stop' recibido desde Twilio.")
                    break

        except Exception as e:
            logger.error(f"❌ WebSocket error: %s", e, exc_info=True)
        finally:
            await self._shutdown()

    def _get_greeting_by_time(self):
        """
        Genera saludo según la hora actual en Cancún.
        """
        now = get_cancun_time()
        hour = now.hour
        minute = now.minute
        # Lógica de saludo
        if 3 <= hour < 12:
            return "¡Buenos días!, Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"
        elif hour >= 20 or hour < 3 or (hour == 19 and minute >= 30):
            return "¡Buenas noches!, Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"
        else:
            return "¡Buenas tardes!, Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudarle?"

    def _stt_callback(self, transcript: str, is_final: bool):
        """
        Callback llamado desde deepgram_stt_streamer cuando llega transcripción del STT.
        """
        if not transcript:
            return

        # Actualizamos última vez que recibimos algo
        self.last_partial_time = time.time()

        if is_final:
            logger.info(f"🎙️ USUARIO (final): {transcript}")
            self.last_final_time = time.time()

            # Cancelar GPT anterior si seguía vivo
            if self.current_gpt_task and not self.current_gpt_task.done():
                self.current_gpt_task.cancel()
                logger.info("🧹 GPT anterior cancelado.")

            # ─────────────────────────────────────────────────────────────────
            # Si estamos en modo acumulación, no llamamos a GPT inmediatamente;
            # en vez de eso, vamos sumando transcripciones.
            # ─────────────────────────────────────────────────────────────────
            if self.accumulating_mode:
                self._accumulate_transcript(transcript)
            else:
                # Modo normal: enviamos a GPT de inmediato
                self.current_gpt_task = asyncio.create_task(
                    self.process_gpt_response(transcript)
                )

    # ─────────────────────────────────────────────────────────────────────────
    # LÓGICA “MODO ACUMULACIÓN DE TRANSCRIPCIONES”
    # ─────────────────────────────────────────────────────────────────────────

    def _activate_accumulating_mode(self):
        """
        Se activa cuando la IA pida el número de Whatsapp.
        """
        logger.info("🔵 Activando modo ACUMULACIÓN DE TRANSCRIPCIONES")
        self.accumulating_mode = True
        self.accumulated_transcripts = []
        self._cancel_accumulating_timer()

    def _accumulate_transcript(self, transcript: str):
        """
        Guarda el transcript en accumulated_transcripts
        y REINICIA la cuenta de X segundos.
        """
        self.accumulated_transcripts.append(transcript)
        self._reset_accumulating_timer()
        logger.info(f"🔄 Fragmento acumulado: {transcript} | Total: {len(self.accumulated_transcripts)}")

    def _reset_accumulating_timer(self):
        """
        Reinicia la tarea que espera Xs sin nuevos transcripts para “soltarlo” a GPT.
        """
        self._cancel_accumulating_timer()
        loop = asyncio.get_event_loop()
        self.accumulating_timer_task = loop.create_task(self._accumulating_timer())
        logger.info(f"⏳ Temporizador reiniciado ({self.accumulating_timeout_seconds}s) por nuevo fragmento.")

    def _cancel_accumulating_timer(self):
        """
        Cancela la tarea que espera Xs, si existe.
        """
        if self.accumulating_timer_task and not self.accumulating_timer_task.done():
            self.accumulating_timer_task.cancel()
            self.accumulating_timer_task = None

    async def _accumulating_timer(self):
        """
        Tarea asíncrona que espera self.accumulating_timeout_seconds (ej. 3s).
        Si transcurre ese tiempo sin un nuevo final, “flush” al GPT.
        """
        try:
            await asyncio.sleep(self.accumulating_timeout_seconds)
            logger.info("🟠 Tiempo agotado sin nuevos fragmentos. Flusheando...")
            self._flush_accumulated_transcripts()
        except asyncio.CancelledError:
            logger.debug("🔁 Temporizador de acumulación cancelado (nuevo fragmento llegó).")

    def _flush_accumulated_transcripts(self):
        """
        Si estamos acumulando y pasa el tiempo, se disparará esta función
        para mandar todo el texto junto a GPT.
        """
        if not self.accumulating_mode:
            return

        raw_text = " ".join(self.accumulated_transcripts).strip()
        logger.info(f"🟡 Flushing transcripts acumulados: {raw_text}")

        self.accumulating_mode = False
        self.accumulated_transcripts = []
        self._cancel_accumulating_timer()

        final_text = raw_text.replace(',', '').replace('.', '')

        if final_text:
            logger.info(f"✅ Enviando a GPT: {final_text}")
            self.current_gpt_task = asyncio.create_task(
                self.process_gpt_response(final_text)
            )

    # ─────────────────────────────────────────────────────────────────────────
    # FIN LÓGICA DE ACUMULACIÓN
    # ─────────────────────────────────────────────────────────────────────────

    async def process_gpt_response(self, user_text: str):
        """
        Envía el texto a la IA y reproduce la respuesta por TTS.
        """
        if self.call_ended or not self.websocket or self.websocket.client_state != WebSocketState.CONNECTED:
            return


        user_lang = self._detect_language(user_text)
        self.current_language = user_lang
        self.conversation_history.append({
        "role": "user",
        "content": f"[{user_lang.upper()}] {user_text}"
        } )
  

        # Generar respuesta con el historial actualizado
        gpt_response = generate_openai_response(self.conversation_history)

        if gpt_response == "__END_CALL__":
            # IA dice que hay que colgar
            await self._shutdown()
            return


        self.conversation_history.append({"role": "assistant", "content": gpt_response})
        logger.info(f"🤖 IA (texto completo): {gpt_response}")

        resp_lower = gpt_response.lower()

        # Activa la acumulación si GPT pregunta por el número de WhatsApp
        if "número de whatsapp" in resp_lower:
            self._activate_accumulating_mode()
            self.expecting_number = True
            self.expecting_name = False
        elif any(kw in resp_lower for kw in ["¿es correcto", "¿cuál es el motivo", "¿confirmamos"]):
            self.expecting_number = False
            self.expecting_name = False

        # ─────────────────────────────────────────────────────────────────────
        # DETECCIÓN DE FRASE DE DESPEDIDA
        # ─────────────────────────────────────────────────────────────────────
        if "fue un placer atenderle. que tenga un excelente día. ¡hasta luego!" in resp_lower:
            logger.info("🧼 Frase de cierre detectada. Reproduciendo despedida y terminando llamada.")

            # 1) Generamos TTS
            self.is_speaking = True
            tts_audio = text_to_speech(gpt_response)
            await self._play_audio_bytes(tts_audio)

            # 2) Esperamos 5s para asegurar que el usuario escuche todo
            await asyncio.sleep(5)
            self.is_speaking = False

            # 3) Cerramos la llamada
            await self._shutdown()
            return

        # ─────────────────────────────────────────────────────────────────────
        # REPRODUCIR RESPUESTA NORMAL
        # ─────────────────────────────────────────────────────────────────────
        self.is_speaking = True
        tts_audio = text_to_speech(gpt_response)
        await self._play_audio_bytes(tts_audio)
        # Pausa opcional según la duración del audio
        await asyncio.sleep(len(tts_audio) / 6400)
        self.is_speaking = False

    async def _play_audio_bytes(self, audio_bytes: bytes):
        """
        Envía 'media' con payload base64 a Twilio.
        """
        if not self.stream_sid or self.call_ended:
            return
        if self.websocket and self.websocket.client_state == WebSocketState.CONNECTED:
            encoded = base64.b64encode(audio_bytes).decode("utf-8")
            message = {
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": encoded}
            }
            try:
                await self.websocket.send_text(json.dumps(message))
            except Exception as e:
                logger.error(f"❌ Error enviando audio TTS: {e}", exc_info=True)

    async def _shutdown(self):
        """
        Detener STT, cerrar websocket, limpiar estado.
        """
        if self.call_ended:
            return

        logger.info("📴 Iniciando cierre de llamada...")
        self.call_ended = True

        # Cancelar la tarea de GPT si sigue viva
        if self.current_gpt_task and not self.current_gpt_task.done():
            self.current_gpt_task.cancel()

        # Cerrar STT
        if self.stt_streamer:
            await self.stt_streamer.close()

        # Cerrar websocket
        if self.websocket and self.websocket.client_state == WebSocketState.CONNECTED:
            await self.websocket.close()
            self.conversation_history.clear()

        # Limpiar cachés
        free_slots_cache.clear()
        global last_cache_update
        last_cache_update = None
        from consultarinfo import clear_consultorio_data_cache
        clear_consultorio_data_cache()

        global CURRENT_CALL_MANAGER
        CURRENT_CALL_MANAGER = None

        logger.info("✅ Llamada finalizada y recursos limpiados.")

    async def _monitor_call_timeout(self):
        """
        Cada 5s revisa:
          - Si se llegó al tiempo máximo (10 min).
          - Si hay silencio total de 30s.
        De cumplirse, cierra la llamada.
        """
        while not self.call_ended:
            await asyncio.sleep(5)
            now = time.time()

            # fin por duración máxima
            if now - self.stream_start_time > CALL_MAX_DURATION:
                logger.info("⏱️ Tiempo máximo alcanzado. Terminando llamada.")
                await self._shutdown()
                break

            # fin por silencio total
            if now - self.last_final_time > CALL_SILENCE_TIMEOUT:
                logger.info("🤫 Silencio prolongado. Terminando llamada.")
                await self._shutdown()
                break
